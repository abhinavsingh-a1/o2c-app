import os

import httpx
import litellm_common
import pytest
from litellm_common import CostMeter
from litellm_common import eval_key as real_eval_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [("sk-eval-123", "sk-eval-123"), ("sk-replace-me", None), (None, None)],
)
def test_eval_key_ignores_placeholders(monkeypatch, value, expected):
    monkeypatch.setattr(litellm_common, "setting", lambda name: value)

    assert real_eval_key() == expected


def test_use_eval_key_replaces_the_master_key(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")
    monkeypatch.setattr(litellm_common, "eval_key", lambda: "sk-eval")

    assert litellm_common.use_eval_key() is True
    assert os.environ["LITELLM_MASTER_KEY"] == "sk-eval"


def test_use_eval_key_keeps_the_master_key_without_an_eval_key(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")

    assert litellm_common.use_eval_key() is False
    assert os.environ["LITELLM_MASTER_KEY"] == "sk-master"


def fake_litellm(request: httpx.Request) -> httpx.Response:
    costs = {"/a": "0.25", "/b": "0.5"}
    headers = (
        {"x-litellm-response-cost": costs[request.url.path]}
        if (request.url.path in costs)
        else {}
    )
    return httpx.Response(200, headers=headers)


def test_cost_meter_sums_the_cost_of_every_client_while_active():
    meter = CostMeter()
    one = httpx.Client(transport=httpx.MockTransport(fake_litellm))
    two = httpx.Client(transport=httpx.MockTransport(fake_litellm))

    with meter.active():
        one.get("http://litellm/a")
        two.get("http://litellm/b")
        one.get("http://erp/no-cost")
    one.get("http://litellm/a")  # after the run: not counted

    assert meter.calls == 2
    assert meter.cost_usd == 0.75


def test_cost_meter_restores_httpx_after_an_error():
    original = httpx.Client.send

    with pytest.raises(RuntimeError), CostMeter().active():
        raise RuntimeError("boom")

    assert httpx.Client.send is original


def test_cost_meter_also_counts_httpx2_clients():
    httpx2 = pytest.importorskip("httpx2")  # used by the OpenAI SDK and ChatOpenAI
    meter = CostMeter()
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, headers={"x-litellm-response-cost": "0.125"}
            )
        )
    )

    with meter.active():
        client.get("http://litellm/chat")

    assert (meter.calls, meter.cost_usd) == (1, 0.125)
    assert httpx2.Client in litellm_common.client_classes()
