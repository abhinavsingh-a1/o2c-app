from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from o2c_common.schema import PurchaseOrder
from pydantic import BaseModel
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from agent.state import AgentState, Issue

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


class ReviewSummary(BaseModel):
    """One row in the review queue list."""

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
    """Everything the detail page shows for one order."""

    awaiting_review: bool
    status: str
    explanation: str | None
    issues: list[Issue]
    purchase_order: PurchaseOrder
    customer_number: str | None
    sales_order_number: str | None
    source_document: dict[str, Any]


class DecisionResult(BaseModel):
    """What the API returns after a person's decision."""

    status: str
    sales_order_number: str | None
    issues: list[Issue]
    explanation: str | None


class ReviewStore:
    def __init__(self, database_url: str):
        self._engine = create_engine(database_url)
        _metadata.create_all(self._engine)

    def upsert(self, state: AgentState) -> None:
        """Insert the order, or update it if it is already in the queue."""
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

    def list(self, status: str | None) -> list[ReviewSummary]:
        stmt = select(review_queue).order_by(review_queue.c.updated_at.desc())
        if status:
            stmt = stmt.where(review_queue.c.status == status)
        with self._engine.connect() as conn:
            return [
                ReviewSummary.model_validate(dict(row._mapping))
                for row in conn.execute(stmt)
            ]
