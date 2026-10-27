from datetime import date
from decimal import Decimal

import httpx
import pytest
from agent.anomaly import anomaly_score
from agent.contracts import Tolerance, ToleranceLookup
from agent.erp import ErpClient, ErpRejected
from agent.materials import MaterialMatch, MaterialMatcher
from agent.settings import REPO_ROOT
from langchain_core.documents import Document

PROMPTS_DIR = REPO_ROOT / "prompts"

CUSTOMER = {"kunnr": "1000107", "name1": "Salzach Kunststofftechnik GmbH"}
CANDIDATES = [
    {
        "matnr": "400101",
        "maktx": "Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk",
        "meins": "PAK",
    },
    {"matnr": "400120", "maktx": "Stretchfolie 500 mm x 300 m, 23 my", "meins": "ROL"},
    {"matnr": "400103", "maktx": "Keilriemen SPZ 1250", "meins": "PC"},
]


class FakeChain:
    """Stands in for llm.with_structured_output(...): records messages, returns a fixed result."""

    def __init__(self, result):
        self.result = result
        self.calls: list[list[tuple[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.result


# --- ERP client ---


def erp(handler) -> ErpClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://erp")
    return ErpClient("http://erp", client=http)


def test_get_customer_returns_dict_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/customers/1000107"
        return httpx.Response(200, json=CUSTOMER)

    assert erp(handler).get_customer("1000107") == CUSTOMER


def test_get_customer_returns_none_on_404():
    client = erp(lambda request: httpx.Response(404, json={"detail": "not found"}))

    assert client.get_customer("9999999") is None


def test_search_materials_returns_empty_list_on_empty_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/materials"
        assert request.url.params["q"] == "Keilriemen"
        return httpx.Response(200, json=[])

    assert erp(handler).search_materials("Keilriemen") == []


def test_create_order_raises_erp_rejected_on_422():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/salesorders"
        return httpx.Response(422, json={"detail": "unknown customer"})

    with pytest.raises(ErpRejected, match="unknown customer"):
        erp(handler).create_order({"kunnr": "9999999", "items": []})


def test_server_error_raises_http_status_error():
    client = erp(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(httpx.HTTPStatusError):
        client.get_customer("1000107")


class Flaky:
    """MockTransport handler that fails with the given outcomes, then answers 200."""

    def __init__(self, *failures):
        self.failures = list(failures)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, text="unavailable")
        return httpx.Response(200, json=CUSTOMER)


def retrying_erp(handler) -> tuple[ErpClient, list[float]]:
    waits: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://erp")
    return ErpClient("http://erp", client=http, sleep=waits.append), waits


def retries(call: str) -> float:
    from o2c_common.metrics import sample

    return sample("o2c_erp_retries_total", call=call)


@pytest.mark.parametrize(
    "failure",
    [
        502,
        503,
        504,
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
def test_get_retries_transient_errors(failure):
    handler = Flaky(failure, failure)
    client, waits = retrying_erp(handler)
    before = retries("get_customer")

    assert client.get_customer("1000107") == CUSTOMER

    assert len(handler.requests) == 3
    assert len(waits) == 2
    assert retries("get_customer") - before == 2


def test_get_gives_up_after_three_attempts():
    handler = Flaky(503, 503, 503, 503)
    client, waits = retrying_erp(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.get_price("400103", "1000107", date(2026, 1, 1))

    assert len(handler.requests) == 3
    assert len(waits) == 2


def test_get_raises_the_last_transport_error():
    handler = Flaky(*[httpx.ConnectError("refused")] * 3)
    client, _ = retrying_erp(handler)

    with pytest.raises(httpx.ConnectError):
        client.search_materials("Keilriemen")

    assert len(handler.requests) == 3


@pytest.mark.parametrize("status", [500, 404, 400])
def test_get_does_not_retry_other_statuses(status):
    handler = Flaky(status)
    client, waits = retrying_erp(handler)

    try:
        client.get_material("400103")
    except httpx.HTTPStatusError:
        pass

    assert len(handler.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.create_order({"kunnr": "1000107", "items": []}),
        lambda c: c.cancel_order("0000001234"),
    ],
)
@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout("slow")])
def test_posts_are_never_retried(call, failure):
    handler = Flaky(failure)
    client, waits = retrying_erp(handler)

    with pytest.raises((httpx.HTTPStatusError, httpx.ReadTimeout)):
        call(client)

    assert len(handler.requests) == 1
    assert waits == []


def test_backoff_grows_exponentially_with_jitter():
    from agent.erp import BACKOFF_BASE, backoff

    for attempt in (1, 2):
        delay = BACKOFF_BASE * 2 ** (attempt - 1)
        waits = [backoff(attempt) for _ in range(50)]
        assert all(delay <= w <= 2 * delay for w in waits)
        assert len(set(waits)) > 1  # jitter


def test_retry_counter_is_initialised_for_every_read():
    from o2c_common.metrics import ERP_CALLS, REGISTRY

    erp(lambda request: httpx.Response(200, json={}))
    names = {
        s.labels["call"]
        for family in REGISTRY.collect()
        if family.name == "o2c_erp_retries"
        for s in family.samples
        if s.name == "o2c_erp_retries_total"
    }
    assert set(ERP_CALLS) <= names


# --- contract tolerance (RAG) ---


class FakeStore:
    def __init__(self, hits: list[Document]):
        self.hits = hits
        self.searches: list[dict] = []

    def similarity_search(self, query, k=4, filter=None):
        self.searches.append({"query": query, "k": k, "filter": filter})
        return self.hits


CONTRACT_HITS = [
    Document(
        page_content="§4 Prices: deviations up to 2% are accepted.",
        metadata={"customer_number": "1000107", "source": "1000107-contract.md"},
    ),
    Document(
        page_content="§5 Delivery: DAP Salzburg.",
        metadata={"customer_number": "1000107", "source": "1000107-contract.md"},
    ),
]
TOLERANCE = Tolerance(
    tolerance_pct=Decimal(2), evidence="deviations up to 2% are accepted"
)


def test_tolerance_lookup_returns_chain_result():
    store, chain = FakeStore(CONTRACT_HITS), FakeChain(TOLERANCE)

    result = ToleranceLookup(store, chain, PROMPTS_DIR)("1000107")

    assert result == TOLERANCE
    assert store.searches[0]["filter"] == {"customer_number": "1000107"}
    (system, prompt), (human, context) = chain.calls[0]
    assert (system, human) == ("system", "human")
    assert "{schema}" not in prompt
    assert "deviations up to 2%" in context and "DAP Salzburg" in context


def test_tolerance_lookup_caches_per_customer():
    chain = FakeChain(TOLERANCE)
    lookup = ToleranceLookup(FakeStore(CONTRACT_HITS), chain, PROMPTS_DIR)

    first = lookup("1000107")
    second = lookup("1000107")

    assert first == second == TOLERANCE
    assert len(chain.calls) == 1


def test_tolerance_lookup_without_hits_returns_no_contract():
    chain = FakeChain(TOLERANCE)

    result = ToleranceLookup(FakeStore([]), chain, PROMPTS_DIR)("1000999")

    assert result.tolerance_pct is None
    assert result.evidence == "no contract found"
    assert chain.calls == []


# --- material matching ---


def test_material_matcher_returns_valid_candidate():
    match = MaterialMatch(matnr="400103", reason="same V-belt profile and length")

    result = MaterialMatcher(FakeChain(match), PROMPTS_DIR)(
        "Keilriemen SPZ 1250", CANDIDATES
    )

    assert result == match


def test_material_matcher_rejects_invented_number():
    chain = FakeChain(MaterialMatch(matnr="499999", reason="looks right"))

    result = MaterialMatcher(chain, PROMPTS_DIR)("Keilriemen SPZ 1250", CANDIDATES)

    assert result.matnr is None
    assert "499999" in result.reason


def test_material_matcher_prompt_lists_every_candidate():
    chain = FakeChain(MaterialMatch(matnr=None, reason="no match"))

    MaterialMatcher(chain, PROMPTS_DIR)("Rollenkette 08B-1 Simplex, 5 m", CANDIDATES)

    (_, prompt), (_, description) = chain.calls[0]
    assert "{candidates}" not in prompt
    assert '"properties"' not in prompt  # the schema goes to the API (strict mode)
    for c in CANDIDATES:
        assert f"{c['matnr']}: {c['maktx']} ({c['meins']})" in prompt
    assert description == "Rollenkette 08B-1 Simplex, 5 m"


# --- anomaly ---


def test_anomaly_score_is_zero_without_a_model():
    # conftest makes the MLflow model unavailable; the agent must still get a score.
    assert anomaly_score("1000107", "400103", 13.0) == 0.0
