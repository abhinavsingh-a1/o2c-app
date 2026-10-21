import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from agent import explain as explain_module
from agent.explain import explanation_facts, make_explainer
from agent.settings import Settings
from agent.state import Issue, IssueCode

RECORDED = "Line 2 is priced above the agreed price. Please check with the customer."


class FakeChatOpenAI:
    instances: ClassVar[list["FakeChatOpenAI"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = None
        FakeChatOpenAI.instances.append(self)

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=RECORDED)


@pytest.fixture
def fake_llm(monkeypatch):
    FakeChatOpenAI.instances = []
    monkeypatch.setattr(explain_module, "ChatOpenAI", FakeChatOpenAI)
    return FakeChatOpenAI


@pytest.fixture
def settings() -> Settings:
    return Settings(
        litellm_master_key="test",
        events_database_url="postgresql://test:test@localhost/test",
        s3_access_key="test",
        s3_secret_key="test",
        auth_jwt_secret="x" * 32,
        _env_file=None,
    )


@pytest.fixture
def state(make_state, deviating_po):
    issue = Issue(
        code=IssueCode.PRICE_DEVIATION, line_no=1, message="Line 1: price too high."
    )
    return make_state(deviating_po).model_copy(update={"issues": [issue]})


def test_explanation_facts(state):
    facts = explanation_facts(state)

    po = state.purchase_order
    assert facts["po_number"] == po.po_number
    assert facts["total"] == str(po.total_amount)
    assert facts["issues"] == ["Line 1: price too high."]
    assert [line["line_no"] for line in facts["lines"]] == [
        line.line_no for line in po.lines
    ]
    assert set(facts["lines"][0]) == {
        "line_no",
        "description",
        "quantity",
        "unit_price",
    }


def test_explainer_sends_the_facts(fake_llm, settings, state):
    text = make_explainer(settings)(state)

    [llm] = fake_llm.instances
    assert text == RECORDED
    assert llm.kwargs["model"] == "o2c-extract"
    system, human = llm.messages
    assert system[0] == "system" and "finance clerk" in system[1]
    assert json.loads(human[1]) == explanation_facts(state)
    metadata = llm.kwargs["extra_body"]["metadata"]
    assert metadata["trace_name"] == "explain_exception"
    assert "trace_id" not in metadata  # services leave it to LiteLLM


def test_explainer_uses_the_chosen_trace_id(fake_llm, settings, state):
    make_explainer(settings, trace_id_for=lambda s: "f" * 32)(state)

    assert (
        fake_llm.instances[0].kwargs["extra_body"]["metadata"]["trace_id"] == "f" * 32
    )
