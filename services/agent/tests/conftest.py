"""Shared fakes for the agent tests, exposed as fixtures.

Test modules are imported in importlib mode, so they cannot `from conftest import ...`.
"""

import io
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from agent.app import create_app, make_handler
from agent.auth import Authenticator, hash_password
from agent.checkpoint import thread_config
from agent.contracts import Tolerance
from agent.graph import Deps, build_graph
from agent.materials import MaterialMatch
from agent.reviews import AuditEntry, ReviewSummary
from agent.settings import REPO_ROOT, Settings
from agent.state import AgentState, Review
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from o2c_common.events import Envelope
from o2c_common.schema import PurchaseOrder

PO = PurchaseOrder.model_validate_json(
    (REPO_ROOT / "poc" / "data" / "ground_truth" / "PO-0001.json").read_text(
        encoding="utf-8"
    )
)
KEY = "k" * 64  # graph tests: thread of make_state()
ORDER_KEY = "e" * 64  # API tests: thread of make_event()
JWT_SECRET = secrets.token_hex(32)  # random per test run; never a real secret
USERNAME, USER_NAME, PASSWORD = "reviewer1", "Test Reviewer", "correct-horse-battery"


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


class FakeReviewStore:
    """In-memory stand-in for agent.reviews.ReviewStore."""

    def __init__(self):
        self.rows: dict[str, ReviewSummary] = {}
        self.decisions: dict[str, list[AuditEntry]] = {}

    def upsert(self, state: AgentState) -> None:
        po = state.purchase_order
        self.rows[state.idempotency_key] = ReviewSummary(
            idempotency_key=state.idempotency_key,
            po_number=po.po_number,
            customer_name=po.customer_name,
            customer_number=state.customer_number,
            total_amount=po.total_amount,
            currency=po.currency,
            status=state.status,
            issue_codes=[issue.code.value for issue in state.issues],
            updated_at=datetime.now(UTC),
        )

    def query(self, status: str | None) -> list[ReviewSummary]:
        rows = [r for r in self.rows.values() if status is None or r.status == status]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def record_decision(
        self, before: AgentState, review: Review, reviewer_name: str, after: AgentState
    ) -> None:
        entries = self.decisions.setdefault(before.idempotency_key, [])
        entries.append(
            AuditEntry(
                id=sum(len(e) for e in self.decisions.values()) + 1,
                action=review.action,
                reviewer=review.reviewer,
                reviewer_name=reviewer_name,
                comment=review.comment,
                result_status=after.status,
                sales_order_number=after.sales_order_number,
                decided_at=datetime.now(UTC),
                po_after=review.edited_po.model_dump(mode="json")
                if review.edited_po
                else None,
            )
        )

    def history(self, idempotency_key: str) -> list[AuditEntry]:
        return sorted(
            self.decisions.get(idempotency_key, []), key=lambda e: e.decided_at
        )


class FakeS3:
    """get_object from an in-memory dict; unknown keys raise NoSuchKey like S3."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class FakeProducer:
    def __init__(self):
        self.sent: list[tuple[str, Envelope]] = []
        self.flushes = 0

    def send(self, topic, event):
        self.sent.append((topic, event))

    def flush(self, timeout: float = 10.0):
        self.flushes += 1

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.sent]

    def last(self, topic: str) -> Envelope:
        return [event for t, event in self.sent if t == topic][-1]


def no_match(description, candidates):
    return MaterialMatch(matnr=None, reason="no match")


def fake_explain(state: AgentState) -> str:
    return "Needs review: " + ", ".join(issue.code for issue in state.issues)


def change_line(
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


@pytest.fixture
def po() -> PurchaseOrder:
    return PO


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


@pytest.fixture
def review_store() -> FakeReviewStore:
    return FakeReviewStore()


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture
def make_deps(erp):
    """Factory: Deps around the `erp` fixture, with overridable matcher, anomaly score
    and explanation."""

    def make(
        match_material=no_match,
        anomaly=lambda kunnr, matnr, qty: 0.0,
        explain=fake_explain,
    ) -> Deps:
        return Deps(
            erp=erp,
            tolerance=lambda kunnr: Tolerance(tolerance_pct=Decimal(2), evidence="x"),
            match_material=match_material,
            anomaly=anomaly,
            explain=explain,
            auto_post_limit=Decimal(10000),
        )

    return make


@pytest.fixture
def make_graph(make_deps):
    """Factory: graph with an InMemorySaver, so it can pause in human_review.
    Keyword arguments go to make_deps."""

    def make(**deps):
        return build_graph(make_deps(**deps), checkpointer=InMemorySaver())

    return make


@pytest.fixture
def config() -> dict:
    """Thread config of the run started by make_state()."""
    return thread_config(KEY)


@pytest.fixture
def with_line():
    """change_line as a fixture: test modules cannot import from conftest."""
    return change_line


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


# --- API: app, login and orders ---


@pytest.fixture
def document() -> dict:
    """Source document fields of the order created by make_event()."""
    return {
        "s3_bucket": "o2c-documents",
        "s3_key": f"incoming/{ORDER_KEY}.pdf",
        "filename": "PO-0001.pdf",
        "sender": "buyer@example.com",
    }


@pytest.fixture
def make_event(document):
    """Factory: orders.extracted event for a PO (idempotency key ORDER_KEY by default)."""

    def make(po: PurchaseOrder = PO, key: str = ORDER_KEY) -> Envelope:
        return Envelope.new(
            "orders.extracted",
            "extractor",
            key,
            {
                **document,
                "doc_type": "purchase_order",
                "purchase_order": po.model_dump(mode="json"),
            },
        )

    return make


@pytest.fixture
def deviating_po(po, with_line) -> PurchaseOrder:
    return with_line(po, 2, unit_price=Decimal("18.04"))  # agreed 16.40, +10%


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


@pytest.fixture
def users_file(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(
        json.dumps(
            {USERNAME: {"name": USER_NAME, "password_hash": hash_password(PASSWORD)}}
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def credentials() -> dict:
    return {
        "username": USERNAME,
        "name": USER_NAME,
        "password": PASSWORD,
        "jwt_secret": JWT_SECRET,
    }


@pytest.fixture
def app(make_deps, producer, review_store, s3, users_file):
    settings = Settings(
        litellm_master_key="test",
        events_database_url="postgresql://test:test@localhost/test",
        s3_access_key="test",
        s3_secret_key="test",
        auth_jwt_secret=JWT_SECRET,
    )
    return create_app(
        settings=settings,
        deps=make_deps(),
        checkpointer=InMemorySaver(),
        producer=producer,
        review_store=review_store,
        s3=s3,
        auth=Authenticator(users_file, JWT_SECRET),
        start_consumer=False,
    )


@pytest.fixture
def client(app):
    """Not logged in; send auth_headers on every /reviews call."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client) -> dict:
    resp = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def handle(app, client, producer, review_store):
    # The graph is built in the lifespan, so the client must be open first.
    return make_handler(app.state.graph, producer, review_store)


@pytest.fixture
def waiting(handle, producer, make_event, deviating_po) -> str:
    """Idempotency key of an order paused in human_review; the producer is reset."""
    handle(make_event(deviating_po))
    producer.sent.clear()
    return ORDER_KEY
