import hashlib
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.postgres import PostgresSaver
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PermanentError, PostgresStore
from o2c_common.schema import PurchaseOrder
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from agent.anomaly import anomaly_score
from agent.checkpoint import (
    is_awaiting_review,
    resume_with_review,
    run_or_resume,
    thread_config,
)
from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.explain import make_explainer
from agent.graph import Deps, build_graph
from agent.materials import make_material_matcher
from agent.settings import Settings
from agent.state import AgentState, Review

log = logging.getLogger("agent")
DOCUMENT_FIELDS = ("s3_bucket", "s3_key", "filename", "sender")


def make_deps(settings: Settings) -> Deps:
    return Deps(
        erp=ErpClient(settings.erp_base_url),
        tolerance=make_tolerance_lookup(settings, make_store(settings)),
        match_material=make_material_matcher(settings),
        anomaly=anomaly_score,
        explain=make_explainer(settings),
        auto_post_limit=settings.auto_post_limit,
    )


def _event(
    state: AgentState, topic: str, payload: dict[str, Any], parent: Envelope | None
) -> Envelope:
    # Same content -> same event_id, so re-publishing after a crash or replay is deduplicated.
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]
    return Envelope(
        event_id=uuid5(NAMESPACE_URL, f"{state.idempotency_key}/{topic}/{fingerprint}"),
        event_type=topic,
        producer="agent",
        idempotency_key=state.idempotency_key,
        correlation_id=state.correlation_id,
        causation_id=parent.event_id if parent else None,
        payload=payload,
    )


def outcome_events(
    state: AgentState, parent: Envelope | None
) -> list[tuple[str, Envelope]]:
    po = state.purchase_order
    base = {
        "po_number": po.po_number,
        "customer_number": state.customer_number,
        "status": state.status,
        "decision": state.decision,
        "issues": [issue.model_dump(mode="json") for issue in state.issues],
    }
    events = [("orders.validated", base)]
    if state.status == "posted":
        events.append(
            (
                "orders.posted",
                {
                    **base,
                    "sales_order_number": state.sales_order_number,
                    "total_amount": str(po.total_amount),
                    "currency": po.currency,
                },
            )
        )
    elif state.status == "awaiting_review":
        events.append(
            (
                "orders.exceptions",
                {
                    **base,
                    "explanation": state.explanation,
                    "purchase_order": po.model_dump(mode="json"),
                    "source_document": state.source_document,
                },
            )
        )
    return [(topic, _event(state, topic, payload, parent)) for topic, payload in events]


def make_handler(graph, producer: EventProducer):
    def handle(event: Envelope) -> None:
        p = event.payload
        try:
            po = PurchaseOrder.model_validate(p["purchase_order"])
        except (KeyError, ValidationError) as exc:
            raise PermanentError(f"no valid purchase_order in event: {exc}") from exc
        initial = AgentState(
            idempotency_key=event.idempotency_key,
            correlation_id=event.correlation_id,
            source_event_id=str(event.event_id),
            purchase_order=po,
            source_document={k: p.get(k) for k in DOCUMENT_FIELDS},
        )
        state = run_or_resume(graph, initial)
        log.info("%s -> %s", po.po_number, state.status)
        for topic, out in outcome_events(state, event):
            producer.send(topic, out)

    return handle


def create_app(
    settings: Settings | None = None,
    deps: Deps | None = None,
    checkpointer=None,
    producer=None,
    start_consumer: bool = True,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )
        pool = None
        saver = checkpointer
        if (
            saver is None
        ):  # real run: a pool shared safely by the consumer thread and the API
            pool = ConnectionPool(
                settings.events_database_url.replace(
                    "postgresql+psycopg://", "postgresql://"
                ),
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                open=True,
            )
            saver = PostgresSaver(pool)
            saver.setup()
        app.state.graph = build_graph(deps or make_deps(settings), saver)
        app.state.producer = producer or EventProducer(
            settings.kafka_bootstrap, "agent"
        )
        app.state.consumer = None
        if start_consumer:
            consumer = EventConsumer(
                bootstrap=settings.kafka_bootstrap,
                group_id="agent",
                topics=["orders.extracted"],
                handler=make_handler(app.state.graph, app.state.producer),
                producer=app.state.producer,
                store=PostgresStore(settings.events_database_url),
            )
            app.state.consumer = threading.Thread(
                target=consumer.run, name="consumer", daemon=True
            )
            app.state.consumer.start()
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Order agent", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        thread = request.app.state.consumer
        if start_consumer and (thread is None or not thread.is_alive()):
            raise HTTPException(503, "consumer not running")
        return {"status": "ok"}

    @app.get("/reviews/{idempotency_key}")
    def get_review(idempotency_key: str, request: Request) -> dict[str, Any]:
        snapshot = request.app.state.graph.get_state(thread_config(idempotency_key))
        if not snapshot.values:
            raise HTTPException(404, "unknown order")
        state = AgentState.model_validate(snapshot.values)
        return {
            "awaiting_review": is_awaiting_review(snapshot),
            **state.model_dump(
                mode="json",
                include={
                    "status",
                    "explanation",
                    "issues",
                    "purchase_order",
                    "sales_order_number",
                    "source_document",
                    "customer_number",
                },
            ),
        }

    @app.post("/reviews/{idempotency_key}")
    def post_review(
        idempotency_key: str, review: Review, request: Request
    ) -> dict[str, Any]:
        try:
            state = resume_with_review(request.app.state.graph, idempotency_key, review)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        for topic, out in outcome_events(state, None):
            request.app.state.producer.send(topic, out)
        request.app.state.producer.flush()
        return state.model_dump(
            mode="json",
            include={"status", "sales_order_number", "issues", "explanation"},
        )

    return app
