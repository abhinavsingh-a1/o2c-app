from decimal import Decimal
from enum import StrEnum
from typing import Literal

from o2c_common.schema import PurchaseOrder
from pydantic import BaseModel, Field


class IssueCode(StrEnum):
    UNKNOWN_CUSTOMER = "UNKNOWN_CUSTOMER"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"
    NO_AGREED_PRICE = "NO_AGREED_PRICE"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    QUANTITY_ANOMALY = "QUANTITY_ANOMALY"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    ORDER_VERIFY_FAILED = "ORDER_VERIFY_FAILED"


class Issue(BaseModel):
    code: IssueCode
    message: str
    line_no: int | None = None


class LineCheck(BaseModel):
    line_no: int
    material_number: str | None = None
    material_source: Literal["document", "erp_search", "llm"] | None = None
    agreed_price: Decimal | None = None
    tolerance_pct: Decimal | None = None
    deviation_pct: Decimal | None = None
    anomaly_score: float | None = None


class Review(BaseModel):
    action: Literal["approve", "edit", "reject"]
    reviewer: str
    comment: str | None = None
    edited_po: PurchaseOrder | None = None


class AgentState(BaseModel):
    # identity of the document and its journey
    idempotency_key: str
    correlation_id: str
    source_event_id: str
    purchase_order: PurchaseOrder

    # results of the checks
    customer_number: str | None = None
    lines: list[LineCheck] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    # decision and outcome
    decision: Literal["auto_post", "exception"] | None = None
    explanation: str | None = None
    review: Review | None = None
    sales_order_number: str | None = None
    status: Literal["running", "awaiting_review", "posted", "rejected", "failed"] = (
        "running"
    )
