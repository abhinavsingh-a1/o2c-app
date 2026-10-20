import hashlib
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request, Response
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
from agent.reviews import DecisionResult, ReviewDetail, ReviewStore, ReviewSummary
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


def make_handler(graph, producer: EventProducer, reviews: ReviewStore):
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
        reviews.upsert(state)
        log.info("%s -> %s", po.po_number, state.status)
        for topic, out in outcome_events(state, event):
            producer.send(topic, out)

    return handle


def create_app(
    settings: Settings | None = None,
    deps: Deps | None = None,
    checkpointer=None,
    producer=None,
    review_store=None,
    s3=None,
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
        if saver is None:
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
        app.state.reviews = review_store or ReviewStore(settings.events_database_url)
        app.state.s3 = s3 or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
        app.state.consumer = None
        if start_consumer:
            consumer = EventConsumer(
                bootstrap=settings.kafka_bootstrap,
                group_id="agent",
                topics=["orders.extracted"],
                handler=make_handler(
                    app.state.graph, app.state.producer, app.state.reviews
                ),
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

    app = FastAPI(title="Order agent", version="0.2.0", lifespan=lifespan)

    def _state(request: Request, idempotency_key: str):
        snapshot = request.app.state.graph.get_state(thread_config(idempotency_key))
        if not snapshot.values:
            raise HTTPException(404, "unknown order")
        return snapshot, AgentState.model_validate(snapshot.values)

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        thread = request.app.state.consumer
        if start_consumer and (thread is None or not thread.is_alive()):
            raise HTTPException(503, "consumer not running")
        return {"status": "ok"}

    @app.get("/reviews", response_model=list[ReviewSummary])
    def list_reviews(
        request: Request, status: str = "awaiting_review"
    ) -> list[ReviewSummary]:
        return request.app.state.reviews.list(None if status == "all" else status)

    @app.get("/reviews/{idempotency_key}", response_model=ReviewDetail)
    def get_review(idempotency_key: str, request: Request) -> ReviewDetail:
        snapshot, state = _state(request, idempotency_key)
        return ReviewDetail(
            awaiting_review=is_awaiting_review(snapshot),
            status=state.status,
            explanation=state.explanation,
            issues=state.issues,
            purchase_order=state.purchase_order,
            customer_number=state.customer_number,
            sales_order_number=state.sales_order_number,
            source_document=state.source_document,
        )

    @app.get("/reviews/{idempotency_key}/document")
    def get_document(idempotency_key: str, request: Request) -> Response:
        _, state = _state(request, idempotency_key)
        doc = state.source_document
        if not doc.get("s3_key"):
            raise HTTPException(404, "no document stored for this order")
        try:
            obj = request.app.state.s3.get_object(
                Bucket=doc["s3_bucket"], Key=doc["s3_key"]
            )
        except ClientError as exc:
            raise HTTPException(404, "document not found in storage") from exc
        return Response(
            content=obj["Body"].read(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{doc.get("filename", "order.pdf")}"'
            },
        )

    @app.post("/reviews/{idempotency_key}", response_model=DecisionResult)
    def post_review(
        idempotency_key: str, review: Review, request: Request
    ) -> DecisionResult:
        try:
            state = resume_with_review(request.app.state.graph, idempotency_key, review)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        request.app.state.reviews.upsert(state)
        for topic, out in outcome_events(state, None):
            request.app.state.producer.send(topic, out)
        request.app.state.producer.flush()
        return DecisionResult(
            status=state.status,
            sales_order_number=state.sales_order_number,
            issues=state.issues,
            explanation=state.explanation,
        )

    return app
