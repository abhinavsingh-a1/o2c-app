import pytest
from agent.checkpoint import run_or_resume, thread_config

# FakeErp and the erp / make_graph / make_state fixtures live in conftest.py.


@pytest.fixture
def graph(make_graph):
    return make_graph()  # InMemorySaver checkpointer


def test_new_thread_runs_to_posted(graph, erp, make_state):
    result = run_or_resume(graph, make_state())

    assert result.status == "posted"
    assert result.sales_order_number in erp.orders
    assert len(erp.created) == 1


def test_finished_thread_returns_stored_state(graph, erp, make_state):
    first = run_or_resume(graph, make_state())

    again = run_or_resume(graph, make_state())

    assert again == first
    assert len(erp.created) == 1  # not called a second time


def test_crash_after_erp_stored_order_resumes_to_one_order(graph, erp, make_state):
    erp.crash_once.add("create_order")
    with pytest.raises(Exception, match="create_order"):
        run_or_resume(graph, make_state())
    assert len(erp.orders) == 1  # the ERP has the order; the agent never saw the answer
    assert graph.get_state(thread_config(make_state().idempotency_key)).next == (
        "post_order",
    )

    result = run_or_resume(graph, make_state())

    assert result.status == "posted"
    assert len(erp.created) == 2  # post_order was retried ...
    assert len(erp.orders) == 1  # ... but the idempotency key stopped a duplicate
    assert result.sales_order_number in erp.orders


def test_crash_in_verify_resumes_without_reposting(graph, erp, make_state):
    erp.crash_once.add("get_order")
    with pytest.raises(Exception, match="get_order"):
        run_or_resume(graph, make_state())
    config = thread_config(make_state().idempotency_key)
    assert graph.get_state(config).next == ("verify_order",)
    assert len(erp.created) == 1

    result = run_or_resume(graph, make_state())

    assert result.status == "posted"
    assert len(erp.created) == 1  # resumed at verify_order, not post_order
    assert len(erp.orders) == 1
