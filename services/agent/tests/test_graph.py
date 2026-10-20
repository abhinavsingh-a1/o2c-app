from decimal import Decimal

import pytest
from agent.contracts import Tolerance
from agent.graph import Deps, build_graph, run
from agent.materials import MaterialMatch, MaterialMatcher
from agent.settings import REPO_ROOT
from agent.state import AgentState, IssueCode
from o2c_common.schema import PurchaseOrder

PO = PurchaseOrder.model_validate_json(
    (REPO_ROOT / "poc" / "data" / "ground_truth" / "PO-0001.json").read_text(
        encoding="utf-8"
    )
)
KEY = "k" * 64
VBELN = "0000012345"


class FakeErp:
    """In-memory ERP with the master data PO-0001 needs. Records every write."""

    def __init__(self, netwr_override: Decimal | None = None):
        self.customers = {
            "1000107": {"kunnr": "1000107", "name1": "Salzach Kunststofftechnik GmbH"}
        }
        self.materials = {
            "400101": {
                "matnr": "400101",
                "maktx": "Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk",
                "meins": "PAK",
            },
            "400103": {
                "matnr": "400103",
                "maktx": "Keilriemen SPZ 1250",
                "meins": "PC",
            },
            "400120": {
                "matnr": "400120",
                "maktx": "Stretchfolie 500 mm x 300 m, 23 my",
                "meins": "ROL",
            },
            "400150": {
                "matnr": "400150",
                "maktx": "Rollenkette 08B-1 Simplex, 5 m",
                "meins": "PC",
            },
        }
        self.prices = {  # (matnr, kunnr) -> agreed net price
            ("400101", "1000107"): "14.90",
            ("400103", "1000107"): "9.80",
            ("400120", "1000107"): "16.40",
            ("400150", "1000107"): "54.00",
        }
        self.netwr_override = netwr_override
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.orders: dict[str, dict] = {}

    def get_customer(self, kunnr):
        return self.customers.get(kunnr)

    def search_customers(self, name):
        return [
            c for c in self.customers.values() if name.lower() in c["name1"].lower()
        ]

    def get_material(self, matnr):
        return self.materials.get(matnr)

    def search_materials(self, q=""):
        return [m for m in self.materials.values() if q.lower() in m["maktx"].lower()]

    def get_price(self, matnr, kunnr, on):
        kbetr = self.prices.get((matnr, kunnr))
        return (
            None if kbetr is None else {"matnr": matnr, "kunnr": kunnr, "kbetr": kbetr}
        )

    def create_order(self, order):
        self.created.append(order)
        netwr = sum(Decimal(i["kwmeng"]) * Decimal(i["netpr"]) for i in order["items"])
        self.orders[VBELN] = {
            "vbeln": VBELN,
            "status": "OPEN",
            "items": order["items"],
            "netwr": str(
                self.netwr_override if self.netwr_override is not None else netwr
            ),
        }
        return {"vbeln": VBELN}

    def get_order(self, vbeln):
        return self.orders.get(vbeln)

    def cancel_order(self, vbeln):
        self.cancelled.append(vbeln)
        self.orders[vbeln]["status"] = "CANCELLED"
        return self.orders[vbeln]


class FakeChain:
    def __init__(self, result):
        self.result = result

    def invoke(self, messages):
        return self.result


def no_match(description, candidates):
    return MaterialMatch(matnr=None, reason="no match")


def make_deps(
    erp=None, match_material=no_match, anomaly=lambda kunnr, matnr, qty: 0.0
) -> Deps:
    return Deps(
        erp=erp or FakeErp(),
        tolerance=lambda kunnr: Tolerance(tolerance_pct=Decimal(2), evidence="x"),
        match_material=match_material,
        anomaly=anomaly,
        auto_post_limit=Decimal(10000),
    )


def with_line(
    po: PurchaseOrder, line_no: int, keep_total: bool = False, **changes
) -> PurchaseOrder:
    """Copy of the PO with one line changed. Line and order totals follow unless told otherwise."""
    lines = []
    for line in po.lines:
        if line.line_no == line_no:
            line = line.model_copy(update=changes)
            if not keep_total:
                line = line.model_copy(
                    update={"line_total": line.quantity * line.unit_price}
                )
        lines.append(line)
    total = sum((line.line_total for line in lines), Decimal(0))
    return po.model_copy(update={"lines": lines, "total_amount": total})


def process(po: PurchaseOrder, deps: Deps) -> AgentState:
    state = AgentState(
        idempotency_key=KEY, correlation_id=KEY, source_event_id="e1", purchase_order=po
    )
    return run(build_graph(deps), state)


def codes(result: AgentState) -> set[IssueCode]:
    return {issue.code for issue in result.issues}


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


def test_clean_po_is_posted(erp):
    result = process(PO, make_deps(erp))

    assert result.issues == []
    assert result.status == "posted"
    assert result.sales_order_number == VBELN
    assert len(erp.created) == 1
    order = erp.created[0]
    assert order["idempotency_key"] == KEY
    assert order["kunnr"] == "1000107"
    assert [i["matnr"] for i in order["items"]] == [
        "400103",
        "400120",
        "400101",
        "400150",
    ]
    assert erp.cancelled == []


def test_price_above_tolerance_needs_review(erp):
    po = with_line(PO, 2, unit_price=Decimal("18.04"))  # agreed 16.40, +10%

    result = process(po, make_deps(erp))

    assert codes(result) == {IssueCode.PRICE_DEVIATION}
    assert result.issues[0].line_no == 2
    line2 = next(c for c in result.lines if c.line_no == 2)
    assert line2.deviation_pct == Decimal("10.00")
    assert result.status == "awaiting_review"
    assert erp.created == []


def test_unknown_customer(erp):
    po = PO.model_copy(
        update={"customer_number": None, "customer_name": "Unbekannt AG"}
    )

    result = process(po, make_deps(erp))

    assert IssueCode.UNKNOWN_CUSTOMER in codes(result)
    assert result.customer_number is None
    assert result.status == "awaiting_review"
    assert erp.created == []


def test_line_without_material_number_resolved_by_erp_search(erp):
    result = process(PO, make_deps(erp))

    line1 = next(c for c in result.lines if c.line_no == 1)
    assert line1.material_number == "400103"
    assert line1.material_source == "erp_search"
    line2 = next(c for c in result.lines if c.line_no == 2)
    assert line2.material_source == "document"


def test_invented_material_match_is_unknown_material(erp):
    po = with_line(PO, 4, description="Zahnrad Sonderanfertigung")
    # Real matcher with a chain that invents a number: the matcher must reject it.
    matcher = MaterialMatcher(
        FakeChain(MaterialMatch(matnr="499999", reason="looks right")),
        REPO_ROOT / "prompts",
    )

    result = process(po, make_deps(erp, match_material=matcher))

    assert codes(result) == {IssueCode.UNKNOWN_MATERIAL}
    assert result.issues[0].line_no == 4
    line4 = next(c for c in result.lines if c.line_no == 4)
    assert line4.material_number is None
    assert result.status == "awaiting_review"
    assert erp.created == []


def test_line_total_mismatch(erp):
    po = with_line(
        PO, 3, keep_total=True, line_total=Decimal("230.00")
    )  # 15 x 14.90 = 223.50

    result = process(po, make_deps(erp))

    assert codes(result) == {IssueCode.TOTAL_MISMATCH}
    assert result.issues[0].line_no == 3
    assert result.status == "awaiting_review"
    assert erp.created == []


def test_total_above_auto_post_limit(erp):
    po = with_line(PO, 2, quantity=Decimal(1000))  # 1000 x 16.40 = 16,400

    result = process(po, make_deps(erp))

    assert codes(result) == {IssueCode.ABOVE_AUTO_POST_LIMIT}
    assert result.status == "awaiting_review"
    assert erp.created == []


def test_verify_mismatch_cancels_order():
    erp = FakeErp(netwr_override=Decimal("1.00"))

    result = process(PO, make_deps(erp))

    assert len(erp.created) == 1
    assert codes(result) == {IssueCode.ORDER_VERIFY_FAILED}
    assert erp.cancelled == [VBELN]  # saga compensation
    assert result.status == "awaiting_review"


def test_quantity_anomaly(erp):
    result = process(PO, make_deps(erp, anomaly=lambda kunnr, matnr, qty: 0.95))

    assert codes(result) == {IssueCode.QUANTITY_ANOMALY}
    assert {i.line_no for i in result.issues} == {1, 2, 3, 4}
    assert all(c.anomaly_score == 0.95 for c in result.lines)
    assert result.status == "awaiting_review"
    assert erp.created == []
