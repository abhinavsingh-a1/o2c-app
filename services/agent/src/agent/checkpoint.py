from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from agent.state import AgentState, Review


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        yield saver


def thread_config(idempotency_key: str) -> dict:
    return {"configurable": {"thread_id": idempotency_key}}


def is_awaiting_review(snapshot) -> bool:
    return any(task.interrupts for task in snapshot.tasks)


def run_or_resume(graph, initial: AgentState) -> AgentState:
    """Start a new run, resume a crashed one, or return the state of a finished or waiting one."""
    config = thread_config(initial.idempotency_key)
    snapshot = graph.get_state(config)
    if snapshot.next and is_awaiting_review(
        snapshot
    ):  # waiting for a person: do nothing
        return AgentState.model_validate(snapshot.values)
    if snapshot.next:  # crashed half-way: continue from the last checkpoint
        result = graph.invoke(None, config)
    elif snapshot.values:  # finished earlier: return the stored result
        return AgentState.model_validate(snapshot.values)
    else:  # first time we see this PO
        result = graph.invoke(initial, config)
    return AgentState.model_validate(result)


def resume_with_review(graph, idempotency_key: str, review: Review) -> AgentState:
    """Deliver a person's decision to a run that is waiting in human_review."""
    config = thread_config(idempotency_key)
    if not is_awaiting_review(graph.get_state(config)):
        raise ValueError(f"order {idempotency_key} is not waiting for a review")
    result = graph.invoke(Command(resume=review.model_dump(mode="json")), config)
    return AgentState.model_validate(result)
