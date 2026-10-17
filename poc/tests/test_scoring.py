from datetime import date
from decimal import Decimal

import pytest
from schema import LineItem, PurchaseOrder
from scoring import HEADER_FIELDS, LINE_FIELDS, score


def make_line(
    line_no: int, material_number: str | None, price: str, qty: str
) -> LineItem:
    return LineItem(
        line_no=line_no,
        material_number=material_number,
        description=f"Material {line_no}",
        quantity=Decimal(qty),
        unit="PC",
        unit_price=Decimal(price),
        line_total=Decimal(qty) * Decimal(price),
    )


@pytest.fixture
def truth() -> PurchaseOrder:
    lines = [make_line(1, "400101", "14.90", "10"), make_line(2, None, "8.75", "4")]
    return PurchaseOrder(
        po_number="4500012345",
        po_date=date(2026, 10, 15),
        customer_name="Steirische Metallbau GmbH",
        customer_number="1000101",
        currency="EUR",
        delivery_date=date(2026, 11, 2),
        ship_to_address="Industriestraße 12, 8020 Graz, Austria",
        lines=lines,
        total_amount=sum((line.line_total for line in lines), Decimal(0)),
    )


def expected_total(truth: PurchaseOrder) -> int:
    return len(HEADER_FIELDS) + len(LINE_FIELDS) * len(truth.lines)


def test_perfect_match(truth):
    result = score(truth.model_copy(deep=True), truth)
    assert result == {
        "correct": expected_total(truth),
        "total": expected_total(truth),
        "errors": [],
    }


def test_one_wrong_header_field(truth):
    result = score(truth.model_copy(update={"po_number": "4500099999"}), truth)
    assert result["correct"] == expected_total(truth) - 1
    assert result["errors"] == ["po_number"]


def test_missing_line(truth):
    result = score(truth.model_copy(update={"lines": truth.lines[:1]}), truth)
    assert result["total"] == expected_total(truth)
    assert result["correct"] == expected_total(truth) - len(LINE_FIELDS)
    assert result["errors"] == [f"line.{field}" for field in LINE_FIELDS]


def test_extra_line(truth):
    extra = make_line(3, "400125", "129.00", "1")
    result = score(truth.model_copy(update={"lines": [*truth.lines, extra]}), truth)
    assert result["total"] == expected_total(truth)
    assert result["correct"] == expected_total(truth)
    assert result["errors"] == ["extra lines"]


def test_result_none(truth):
    result = score(None, truth)
    assert result == {
        "correct": 0,
        "total": expected_total(truth),
        "errors": ["invalid output"],
    }
