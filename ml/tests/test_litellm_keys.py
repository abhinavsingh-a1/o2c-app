import json

import httpx
import litellm_keys
import litellm_spend
import pytest
from litellm_keys import BUDGET_DURATION, SERVICES, ensure_key, env_name, find_key


class FakeLiteLLM:
    """Recorded behaviour of LiteLLM's /key/list, /key/generate and /key/update."""

    def __init__(self, keys: list[dict] | None = None):
        self.keys = keys or []
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((path, body))
        if path == "/key/list":
            alias = request.url.params.get("key_alias")
            keys = [k for k in self.keys if alias in (None, k["key_alias"])]
            return httpx.Response(200, json={"keys": keys, "total_pages": 1})
        if path == "/key/generate":
            key = f"sk-new-{body['key_alias']}"
            self.keys.append(
                {
                    "token": f"hash-{body['key_alias']}",
                    "key_alias": body["key_alias"],
                    "max_budget": body["max_budget"],
                    "budget_duration": body["budget_duration"],
                    "spend": 0.0,
                }
            )
            return httpx.Response(200, json={"key": key})
        if path == "/key/update":
            for k in self.keys:
                if k["token"] == body["key"]:
                    k.update(max_budget=body["max_budget"])
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://litellm", transport=httpx.MockTransport(self)
        )


def existing(alias: str, budget: float, duration: str = BUDGET_DURATION, **kw) -> dict:
    return {
        "token": f"hash-{alias}",
        "key_alias": alias,
        "max_budget": budget,
        "budget_duration": duration,
        **kw,
    }


def test_the_agreed_budgets():
    assert SERVICES == {"classifier": 1.0, "extractor": 2.0, "agent": 1.0, "eval": 3.0}
    assert BUDGET_DURATION == "1mo"
    assert env_name("eval") == "LITELLM_KEY_EVAL"


def test_find_key_by_alias():
    fake = FakeLiteLLM([existing("agent", 1.0), existing("agent-old", 1.0)])

    assert find_key(fake.client(), "agent")["token"] == "hash-agent"
    assert find_key(fake.client(), "eval") is None


def test_missing_key_is_created_with_its_budget():
    fake = FakeLiteLLM()

    assert ensure_key(fake.client(), "extractor", 2.0) == (
        "created",
        "sk-new-extractor",
    )
    path, body = fake.requests[-1]
    assert path == "/key/generate"
    assert body["key_alias"] == "extractor"
    assert body["max_budget"] == 2.0
    assert body["budget_duration"] == "1mo"


def test_existing_key_is_kept_and_not_shown():
    fake = FakeLiteLLM([existing("agent", 1.0)])

    assert ensure_key(fake.client(), "agent", 1.0) == ("exists", "")
    assert [path for path, _ in fake.requests] == ["/key/list"]


def test_a_changed_budget_is_corrected():
    fake = FakeLiteLLM([existing("eval", 10.0)])

    assert ensure_key(fake.client(), "eval", 3.0) == ("updated", "")
    assert fake.requests[-1] == (
        "/key/update",
        {"key": "hash-eval", "max_budget": 3.0, "budget_duration": "1mo"},
    )


def test_two_keys_with_one_alias_stop_the_script():
    fake = FakeLiteLLM([existing("agent", 1.0), existing("agent", 1.0)])

    with pytest.raises(SystemExit):
        find_key(fake.client(), "agent")


def test_main_is_idempotent_and_prints_new_keys_once(capsys):
    fake = FakeLiteLLM([existing("agent", 1.0)])

    assert litellm_keys.main(fake.client()) == 0
    first = capsys.readouterr().out
    assert "LITELLM_KEY_CLASSIFIER=sk-new-classifier" in first
    assert "LITELLM_KEY_EVAL=sk-new-eval" in first
    assert "LITELLM_KEY_AGENT=" not in first  # existed before

    litellm_keys.main(fake.client())
    second = capsys.readouterr().out
    assert "sk-" not in second
    assert second.count("exists") == 4
    assert len(fake.keys) == 4


def test_spend_table_per_key(capsys):
    fake = FakeLiteLLM(
        [
            existing(
                "extractor", 2.0, spend=0.5, budget_reset_at="2026-11-01T00:00:00"
            ),
            existing("agent", 1.0, spend=0.01),
            {"token": "hash-master", "key_alias": None, "spend": 7.0},
        ]
    )

    assert litellm_spend.main(fake.client()) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["key", "spend", "budget", "used", "period", "resets"]
    assert out[2].split() == ["agent", "$0.0100", "$1.00", "1.0%", "1mo"]
    assert out[3].split() == [
        "extractor",
        "$0.5000",
        "$2.00",
        "25.0%",
        "1mo",
        "2026-11-01",
    ]
    assert out[-1] == "total over 2 keys: $0.5100"


def test_spend_rows_without_budget():
    rows = litellm_spend.spend_rows([{"key_alias": "x", "spend": None}])
    assert rows == [["x", "$0.0000", "none", "", "", ""]]
