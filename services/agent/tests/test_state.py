import json
from pathlib import Path

from agent.state import AgentState, Issue, IssueCode, LineCheck
from o2c_common.schema import PurchaseOrder

PO = PurchaseOrder.model_validate_json(
    Path("poc/data/ground_truth/PO-0001.json").read_text(encoding="utf-8")
)


def make_state() -> AgentState:
    return AgentState(
        idempotency_key="k" * 64,
        correlation_id="k" * 64,
        source_event_id="e1",
        purchase_order=PO,
    )


def test_defaults():
    state = make_state()
    assert state.status == "running"
    assert state.issues == [] and state.lines == []


def test_json_roundtrip_keeps_decimals():
    state = make_state()
    state.lines.append(LineCheck(line_no=1, agreed_price="9.80", deviation_pct="0"))
    state.issues.append(
        Issue(code=IssueCode.PRICE_DEVIATION, message="too high", line_no=1)
    )
    restored = AgentState.model_validate(json.loads(state.model_dump_json()))
    assert restored == state
    assert str(restored.lines[0].agreed_price) == "9.80"
