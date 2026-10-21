from o2c_common.schema import PurchaseOrder

HEADER_FIELDS = [
    "po_number",
    "po_date",
    "customer_name",
    "customer_number",
    "currency",
    "delivery_date",
    "ship_to_address",
    "total_amount",
]
LINE_FIELDS = [
    "material_number",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "line_total",
]


def _same(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()) == " ".join(b.split())
    return a == b


def score(result: PurchaseOrder | None, truth: PurchaseOrder) -> dict:
    """Field-level score: how many expected fields were extracted correctly."""
    total = len(HEADER_FIELDS) + len(LINE_FIELDS) * len(truth.lines)
    if result is None:
        return {"correct": 0, "total": total, "errors": ["invalid output"]}

    correct, errors = 0, []
    for field in HEADER_FIELDS:
        if _same(getattr(result, field), getattr(truth, field)):
            correct += 1
        else:
            errors.append(field)

    got = {line.line_no: line for line in result.lines}
    for expected in truth.lines:
        line = got.get(expected.line_no)
        for field in LINE_FIELDS:
            if line is not None and _same(
                getattr(line, field), getattr(expected, field)
            ):
                correct += 1
            else:
                errors.append(f"line.{field}")

    extra = len(result.lines) - len(truth.lines)
    if extra > 0:
        errors.append("extra lines")
    return {"correct": correct, "total": total, "errors": errors}
