from decimal import Decimal

import pytest
from agent.app import create_app, make_handler
from agent.settings import Settings
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from o2c_common.events import Envelope
from o2c_common.kafka import PermanentError
from o2c_common.schema import PurchaseOrder

# FakeErp and the po / erp / make_deps / with_line fixtures live in conftest.py.

KEY = "e" * 64
DOCUMENT = {
    "s3_bucket": "o2c-documents",
    "s3_key": f"incoming/{KEY}.pdf",
    "filename": "PO-0001.pdf",
    "sender": "buyer@example.com",
}


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


def extracted_event(po: PurchaseOrder) -> Envelope:
    return Envelope.new(
        "orders.extracted",
        "extractor",
        KEY,
        {
            **DOCUMENT,
            "doc_type": "purchase_order",
            "purchase_order": po.model_dump(mode="json"),
        },
    )


@pytest.fixture
def deviating_po(po, with_line) -> PurchaseOrder:
    return with_line(po, 2, unit_price=Decimal("18.04"))  # agreed 16.40, +10%


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


@pytest.fixture
def app(make_deps, producer):
    settings = Settings(
        litellm_master_key="test",
        events_database_url="postgresql://test:test@localhost/test",
    )
    return create_app(
        settings=settings,
        deps=make_deps(),
        checkpointer=InMemorySaver(),
        producer=producer,
        start_consumer=False,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def handle(app, client, producer):
    # The graph is built in the lifespan, so the client must be open first.
    return make_handler(app.state.graph, producer)


@pytest.fixture
def waiting(handle, producer, deviating_po) -> str:
    """Idempotency key of an order paused in human_review; the producer is reset."""
    handle(extracted_event(deviating_po))
    producer.sent.clear()
    return KEY


# --- handler ---


def test_clean_po_publishes_validated_and_posted(handle, producer, erp, po):
    incoming = extracted_event(po)

    handle(incoming)

    assert producer.topics() == ["orders.validated", "orders.posted"]
    posted = producer.last("orders.posted")
    assert posted.payload["status"] == "posted"
    assert posted.payload["sales_order_number"] in erp.orders
    assert posted.payload["total_amount"] == str(po.total_amount)
    assert posted.causation_id == incoming.event_id
    assert posted.idempotency_key == KEY


def test_price_deviation_publishes_validated_and_exception(
    handle, producer, erp, deviating_po
):
    handle(extracted_event(deviating_po))

    assert producer.topics() == ["orders.validated", "orders.exceptions"]
    exception = producer.last("orders.exceptions").payload
    assert exception["status"] == "awaiting_review"
    assert exception["explanation"] == "Needs review: PRICE_DEVIATION"
    assert exception["source_document"] == DOCUMENT
    assert exception["purchase_order"] == deviating_po.model_dump(mode="json")
    assert [i["code"] for i in exception["issues"]] == ["PRICE_DEVIATION"]
    assert erp.created == []


def test_same_event_twice_gives_identical_event_ids(handle, producer, erp, po):
    incoming = extracted_event(po)

    handle(incoming)
    first = [(topic, e.event_id) for topic, e in producer.sent]
    producer.sent.clear()
    handle(incoming)
    second = [(topic, e.event_id) for topic, e in producer.sent]

    assert first == second
    assert len(erp.created) == 1


def test_payload_without_purchase_order_is_permanent_error(handle, producer):
    event = Envelope.new("orders.extracted", "extractor", KEY, dict(DOCUMENT))

    with pytest.raises(PermanentError):
        handle(event)
    assert producer.sent == []


# --- API ---


def test_health_returns_ok(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_get_unknown_review_returns_404(client):
    assert client.get(f"/reviews/{'u' * 64}").status_code == 404


def test_get_waiting_review(client, waiting, deviating_po):
    resp = client.get(f"/reviews/{waiting}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["awaiting_review"] is True
    assert body["status"] == "awaiting_review"
    assert [i["code"] for i in body["issues"]] == ["PRICE_DEVIATION"]
    assert body["source_document"] == DOCUMENT
    assert body["purchase_order"] == deviating_po.model_dump(mode="json")


def test_approve_posts_order_and_publishes(client, waiting, producer, erp):
    resp = client.post(
        f"/reviews/{waiting}",
        json={"action": "approve", "reviewer": "reviewer@example.com"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "posted"
    assert body["sales_order_number"] in erp.orders
    assert producer.topics() == ["orders.validated", "orders.posted"]
    assert (
        producer.last("orders.posted").payload["sales_order_number"]
        == body["sales_order_number"]
    )
    assert producer.flushes == 1
    assert client.get(f"/reviews/{waiting}").json()["awaiting_review"] is False


def test_second_review_returns_409(client, waiting, producer, erp):
    review = {"action": "approve", "reviewer": "reviewer@example.com"}
    assert client.post(f"/reviews/{waiting}", json=review).status_code == 200
    producer.sent.clear()

    resp = client.post(f"/reviews/{waiting}", json=review)

    assert resp.status_code == 409
    assert producer.sent == []
    assert len(erp.created) == 1


def test_reject_sets_status_rejected(client, waiting, producer, erp):
    resp = client.post(
        f"/reviews/{waiting}",
        json={
            "action": "reject",
            "reviewer": "reviewer@example.com",
            "comment": "wrong price",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert producer.topics() == ["orders.validated"]
    assert producer.last("orders.validated").payload["status"] == "rejected"
    assert erp.created == []
