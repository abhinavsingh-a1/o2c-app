from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from agent.state import AgentState


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    # PostgresSaver wants a plain postgresql:// URL, without the SQLAlchemy "+psycopg" part.
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()  # creates the checkpoint tables on first use; safe to call every time
        yield saver


def thread_config(idempotency_key: str) -> dict:
    return {"configurable": {"thread_id": idempotency_key}}


def run_or_resume(graph, initial: AgentState) -> AgentState:
    """Start a new run, resume an interrupted one, or return the result of a finished one."""
    config = thread_config(initial.idempotency_key)
    snapshot = graph.get_state(config)
    if snapshot.next:  # a run exists and has nodes left: it crashed or stopped half-way
        result = graph.invoke(None, config)  # None = continue from the last checkpoint
    elif snapshot.values:  # a finished run exists: nothing to do again
        return AgentState.model_validate(snapshot.values)
    else:  # first time we see this PO
        result = graph.invoke(initial, config)
    return AgentState.model_validate(result)
