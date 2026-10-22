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
