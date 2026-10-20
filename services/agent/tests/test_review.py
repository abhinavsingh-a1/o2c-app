from decimal import Decimal

import pytest
from agent.checkpoint import is_awaiting_review, resume_with_review, run_or_resume
from agent.materials import MaterialMatch, MaterialMatcher
from agent.settings import REPO_ROOT
from agent.state import AgentState, IssueCode, Review
from o2c_common.schema import PurchaseOrder

# FakeErp and the po / erp / make_graph / make_state / config / with_line fixtures
# live in conftest.py.

REVIEWER = "reviewer@example.com"


class FakeChain:
    def __init__(self, result):
        self.result = result

    def invoke(self, messages):
        return self.result


def codes(state: AgentState) -> set[IssueCode]:
    return {issue.code for issue in state.issues}


@pytest.fixture
def deviating_po(po, with_line) -> PurchaseOrder:
    return with_line(po, 2, unit_price=Decimal("18.04"))  # agreed 16.40, +10%


@pytest.fixture
def graph(make_graph):
    return make_graph()


@pytest.fixture
def paused(graph, make_state, deviating_po) -> AgentState:
    """A run with a price deviation, waiting in human_review."""
    return run_or_resume(graph, make_state(deviating_po))


def review(action: str, **kwargs) -> Review:
    return Review(action=action, reviewer=REVIEWER, **kwargs)


def test_price_deviation_pauses_for_review(graph, config, erp, paused):
    snapshot = graph.get_state(config)

    assert paused.status == "awaiting_review"
    assert codes(paused) == {IssueCode.PRICE_DEVIATION}
    assert paused.explanation == "Needs review: PRICE_DEVIATION"
    assert is_awaiting_review(snapshot)
    assert snapshot.next == ("human_review",)
    assert erp.created == []


def test_approve_posts_the_order(graph, config, erp, paused):
    result = resume_with_review(graph, paused.idempotency_key, review("approve"))

    assert result.status == "posted"
    assert result.review.action == "approve"
    assert result.sales_order_number in erp.orders
    assert len(erp.created) == 1
    assert (
        erp.created[0]["items"][1]["netpr"] == "18.04"
    )  # the reviewer accepted the price
    assert not is_awaiting_review(graph.get_state(config))


def test_reject_ends_without_order(graph, config, erp, paused):
    result = resume_with_review(
        graph, paused.idempotency_key, review("reject", comment="wrong price")
    )

    assert result.status == "rejected"
    assert result.sales_order_number is None
    assert erp.created == []
    assert graph.get_state(config).next == ()


def test_edit_with_corrected_po_reruns_checks_and_posts(graph, erp, po, paused):
    result = resume_with_review(
        graph, paused.idempotency_key, review("edit", edited_po=po)
    )

    assert result.status == "posted"
    assert result.issues == []
    assert result.purchase_order == po
    assert {c.line_no: c.deviation_pct for c in result.lines}[2] == Decimal("0.00")
    assert len(erp.created) == 1
    assert erp.created[0]["items"][1]["netpr"] == "16.40"


def test_edit_with_still_wrong_po_pauses_again(
    graph, config, erp, po, with_line, paused
):
    still_wrong = with_line(po, 2, unit_price=Decimal("17.50"))  # +6.7%

    result = resume_with_review(
        graph, paused.idempotency_key, review("edit", edited_po=still_wrong)
    )

    assert result.status == "awaiting_review"
    assert codes(result) == {IssueCode.PRICE_DEVIATION}
    assert result.purchase_order == still_wrong
    assert is_awaiting_review(graph.get_state(config))
    assert erp.created == []


def test_approve_with_unknown_material_is_not_possible(
    make_graph, make_state, config, erp, po, with_line
):
    # The matcher's chain invents a number, so line 4 stays without a material.
    matcher = MaterialMatcher(
        FakeChain(MaterialMatch(matnr="499999", reason="looks right")),
        REPO_ROOT / "prompts",
    )
    graph = make_graph(match_material=matcher)
    paused = run_or_resume(
        graph, make_state(with_line(po, 4, description="Zahnrad Sonderanfertigung"))
    )
    assert codes(paused) == {IssueCode.UNKNOWN_MATERIAL}

    result = resume_with_review(graph, paused.idempotency_key, review("approve"))

    assert result.issues[-1].code == IssueCode.APPROVAL_NOT_POSSIBLE
    assert result.status == "awaiting_review"
    assert is_awaiting_review(graph.get_state(config))
    assert erp.created == []


def test_resume_with_review_on_posted_order_raises(graph, make_state):
    posted = run_or_resume(graph, make_state())
    assert posted.status == "posted"

    with pytest.raises(ValueError, match="not waiting for a review"):
        resume_with_review(graph, posted.idempotency_key, review("approve"))


def test_failing_explain_falls_back_to_issue_list(make_graph, make_state, deviating_po):
    def broken_explain(state):
        raise RuntimeError("LLM unavailable")

    result = run_or_resume(make_graph(explain=broken_explain), make_state(deviating_po))

    assert result.status == "awaiting_review"
    assert result.explanation == "\n".join(f"- {i.message}" for i in result.issues)
    assert result.explanation.startswith("- Line 2: price 18.04 vs agreed 16.40")


def test_run_or_resume_on_waiting_order_changes_nothing(
    make_graph, make_state, config, erp, deviating_po
):
    explained: list[AgentState] = []

    def counting_explain(state):
        explained.append(state)
        return "explained"

    graph = make_graph(explain=counting_explain)
    first = run_or_resume(graph, make_state(deviating_po))
    checkpoint_id = graph.get_state(config).config["configurable"]["checkpoint_id"]

    again = run_or_resume(graph, make_state(deviating_po))

    assert again == first
    assert len(explained) == 1  # the graph did not run again
    assert (
        graph.get_state(config).config["configurable"]["checkpoint_id"] == checkpoint_id
    )
    assert is_awaiting_review(graph.get_state(config))
    assert erp.created == []
