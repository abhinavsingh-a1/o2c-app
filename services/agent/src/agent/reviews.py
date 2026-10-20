from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from o2c_common.schema import PurchaseOrder
from pydantic import BaseModel
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from agent.state import AgentState, Issue, Review

_metadata = MetaData()
review_queue = Table(
    "review_queue",
    _metadata,
    Column("idempotency_key", String(64), primary_key=True),
    Column("po_number", String(35), nullable=False),
    Column("customer_name", String(120), nullable=False),
    Column("customer_number", String(10)),
    Column("total_amount", Numeric(14, 2), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(20), nullable=False, index=True),
    Column("issue_codes", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
review_decisions = Table(  # append-only audit trail: rows are never updated or deleted
    "review_decisions",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("idempotency_key", String(64), nullable=False, index=True),
    Column("po_number", String(35), nullable=False),
    Column("action", String(10), nullable=False),
    Column("reviewer", String(64), nullable=False),
    Column("reviewer_name", String(120), nullable=False),
    Column("comment", Text),
    Column("po_before", JSONB, nullable=False),
    Column("po_after", JSONB),
    Column("result_status", String(20), nullable=False),
    Column("sales_order_number", String(10)),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)


class ReviewSummary(BaseModel):
    idempotency_key: str
    po_number: str
    customer_name: str
    customer_number: str | None
    total_amount: Decimal
    currency: str
    status: str
    issue_codes: list[str]
    updated_at: datetime


class ReviewDetail(BaseModel):
    awaiting_review: bool
    status: str
    explanation: str | None
    issues: list[Issue]
    purchase_order: PurchaseOrder
    customer_number: str | None
    sales_order_number: str | None
    source_document: dict[str, Any]


class ReviewRequest(BaseModel):
    """What the UI sends. The reviewer is taken from the login, never from the request."""

    action: Literal["approve", "edit", "reject"]
    comment: str | None = None
    edited_po: PurchaseOrder | None = None


class DecisionResult(BaseModel):
    status: str
    sales_order_number: str | None
    issues: list[Issue]
    explanation: str | None


class AuditEntry(BaseModel):
    id: int
    action: str
    reviewer: str
    reviewer_name: str
    comment: str | None
    result_status: str
    sales_order_number: str | None
    decided_at: datetime
    po_after: dict[str, Any] | None


class ReviewStore:
    def __init__(self, database_url: str):
        self._engine = create_engine(database_url)
        _metadata.create_all(self._engine)

    def upsert(self, state: AgentState) -> None:
        po, now = state.purchase_order, datetime.now(UTC)
        values = {
            "idempotency_key": state.idempotency_key,
            "po_number": po.po_number,
            "customer_name": po.customer_name,
            "customer_number": state.customer_number,
            "total_amount": po.total_amount,
            "currency": po.currency,
            "status": state.status,
            "issue_codes": [issue.code.value for issue in state.issues],
            "created_at": now,
            "updated_at": now,
        }
        stmt = insert(review_queue).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["idempotency_key"],
            set_={
                k: stmt.excluded[k]
                for k in values
                if k not in ("idempotency_key", "created_at")
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def query(self, status: str | None) -> list[ReviewSummary]:
        stmt = select(review_queue).order_by(review_queue.c.updated_at.desc())
        if status:
            stmt = stmt.where(review_queue.c.status == status)
        with self._engine.connect() as conn:
            return [
                ReviewSummary.model_validate(dict(row._mapping))
                for row in conn.execute(stmt)
            ]

    def record_decision(
        self, before: AgentState, review: Review, reviewer_name: str, after: AgentState
    ) -> None:
        values = {
            "idempotency_key": before.idempotency_key,
            "po_number": before.purchase_order.po_number,
            "action": review.action,
            "reviewer": review.reviewer,
            "reviewer_name": reviewer_name,
            "comment": review.comment,
            "po_before": before.purchase_order.model_dump(mode="json"),
            "po_after": review.edited_po.model_dump(mode="json")
            if review.edited_po
            else None,
            "result_status": after.status,
            "sales_order_number": after.sales_order_number,
            "decided_at": datetime.now(UTC),
        }
        with self._engine.begin() as conn:
            conn.execute(review_decisions.insert().values(**values))

    def history(self, idempotency_key: str) -> list[AuditEntry]:
        stmt = (
            select(review_decisions)
            .where(review_decisions.c.idempotency_key == idempotency_key)
            .order_by(review_decisions.c.decided_at)
        )
        with self._engine.connect() as conn:
            return [
                AuditEntry.model_validate(dict(row._mapping))
                for row in conn.execute(stmt)
            ]
