from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class LineItem(BaseModel):
    line_no: int
    material_number: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


class PurchaseOrder(BaseModel):
    po_number: str
    po_date: date
    customer_name: str
    customer_number: str | None
    currency: str
    delivery_date: date | None
    ship_to_address: str
    lines: list[LineItem]
    total_amount: Decimal
