"""Shared fakes for the agent tests, exposed as fixtures.

Test modules are imported in importlib mode, so they cannot `from conftest import ...`.
"""

from decimal import Decimal

import pytest
from agent.contracts import Tolerance
from agent.graph import Deps
from agent.materials import MaterialMatch
from agent.settings import REPO_ROOT
from agent.state import AgentState
from o2c_common.schema import PurchaseOrder

PO = PurchaseOrder.model_validate_json(
    (REPO_ROOT / "poc" / "data" / "ground_truth" / "PO-0001.json").read_text(
        encoding="utf-8"
    )
)
KEY = "k" * 64


class Crash(Exception):
    """Simulated process crash or lost connection, injected into a FakeErp call."""


class FakeErp:
    """In-memory ERP with the master data PO-0001 needs. Records every write.

    Like the real ERP, create_order is idempotent: an order with a known
    idempotency_key returns the stored order instead of creating a new one.
    """

    def __init__(self):
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
        self.netwr_override: Decimal | None = None
        self.crash_once: set[str] = (
            set()
        )  # method names that raise Crash on their next call
        self.created: list[dict] = []  # every create_order call, including repeats
        self.cancelled: list[str] = []
        self.orders: dict[str, dict] = {}  # vbeln -> stored order

    def _maybe_crash(self, method: str) -> None:
        if method in self.crash_once:
            self.crash_once.discard(method)
            raise Crash(method)

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
        for existing in self.orders.values():
            if existing["idempotency_key"] == order["idempotency_key"]:
                return existing
        netwr = sum(Decimal(i["kwmeng"]) * Decimal(i["netpr"]) for i in order["items"])
        vbeln = f"{len(self.orders) + 12345:010d}"
        self.orders[vbeln] = {
            "vbeln": vbeln,
            "status": "OPEN",
            "idempotency_key": order["idempotency_key"],
            "items": order["items"],
            "netwr": str(
                self.netwr_override if self.netwr_override is not None else netwr
            ),
        }
        self._maybe_crash(
            "create_order"
        )  # stored, but the caller never sees the answer
        return self.orders[vbeln]

    def get_order(self, vbeln):
        self._maybe_crash("get_order")
        return self.orders.get(vbeln)

    def cancel_order(self, vbeln):
        self.cancelled.append(vbeln)
        self.orders[vbeln]["status"] = "CANCELLED"
        return self.orders[vbeln]


def no_match(description, candidates):
    return MaterialMatch(matnr=None, reason="no match")


@pytest.fixture
def po() -> PurchaseOrder:
    return PO


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


@pytest.fixture
def make_deps(erp):
    """Factory: Deps around the `erp` fixture, with overridable matcher and anomaly score."""

    def make(match_material=no_match, anomaly=lambda kunnr, matnr, qty: 0.0) -> Deps:
        return Deps(
            erp=erp,
            tolerance=lambda kunnr: Tolerance(tolerance_pct=Decimal(2), evidence="x"),
            match_material=match_material,
            anomaly=anomaly,
            auto_post_limit=Decimal(10000),
        )

    return make


@pytest.fixture
def make_state():
    """Factory: initial AgentState for a PO, with a fixed idempotency key."""

    def make(po: PurchaseOrder = PO) -> AgentState:
        return AgentState(
            idempotency_key=KEY,
            correlation_id=KEY,
            source_event_id="e1",
            purchase_order=po,
        )

    return make
