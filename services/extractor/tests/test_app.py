import io
import json
from typing import ClassVar

import extractor.app
import pytest
from botocore.exceptions import ClientError
from extractor.app import (
    REPO_ROOT,
    Settings,
    create_app,
    load_prompt,
    make_extractor,
    make_handler,
)
from fastapi.testclient import TestClient
from o2c_common.events import Envelope
from o2c_common.kafka import PermanentError
from o2c_common.schema import PurchaseOrder

PDF_PATH = REPO_ROOT / "poc" / "data" / "pos" / "PO-0001.pdf"
GROUND_TRUTH = REPO_ROOT / "poc" / "data" / "ground_truth" / "PO-0001.json"
PROMPTS_DIR = REPO_ROOT / "prompts"


class FakeExtract:
    """Stands in for the LangChain extractor: returns the recorded ground truth."""

    def __init__(self):
        self.po = PurchaseOrder.model_validate_json(
            GROUND_TRUTH.read_text(encoding="utf-8")
        )
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, session_id: str) -> PurchaseOrder:
        self.calls.append((text, session_id))
        return self.po


class FakeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class FakeProducer:
    def __init__(self):
        self.sent: list[tuple[str, Envelope]] = []

    def send(self, topic, event):
        self.sent.append((topic, event))


def classified_event(doc_type: str = "purchase_order") -> Envelope:
    return Envelope.new(
        "docs.classified",
        "classifier",
        "a" * 32,
        {
            "s3_bucket": "o2c-documents",
            "s3_key": "incoming/abc.pdf",
            "filename": "PO-0001.pdf",
            "source": "email",
            "sender": "buyer@example.com",
            "size_bytes": 1234,
            "doc_type": doc_type,
            "reason": "Customer orders goods from Alpentech.",
        },
    )


@pytest.fixture
def received() -> Envelope:
    return classified_event()


@pytest.fixture
def s3(received) -> FakeS3:
    p = received.payload
    return FakeS3({(p["s3_bucket"], p["s3_key"]): PDF_PATH.read_bytes()})


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


@pytest.fixture
def extract() -> FakeExtract:
    return FakeExtract()


@pytest.fixture
def client(extract):
    settings = Settings(
        s3_access_key="test",
        s3_secret_key="test",
        litellm_master_key="test",
        events_database_url="postgresql://test:test@localhost/test",
    )
    with TestClient(
        create_app(settings=settings, start_consumer=False, extract=extract)
    ) as c:
        yield c


# --- handler ---


def test_purchase_order_emits_extracted_child_event(received, s3, extract, producer):
    make_handler(s3, extract, producer)(received)

    assert len(producer.sent) == 1
    topic, event = producer.sent[0]
    assert topic == "orders.extracted"
    assert event.event_type == "orders.extracted"
    assert event.producer == "extractor"

    p = event.payload
    assert p["purchase_order"] == extract.po.model_dump(mode="json")
    for field in ("s3_bucket", "s3_key", "filename", "source", "sender", "doc_type"):
        assert p[field] == received.payload[field]
    assert "reason" not in p

    text, session_id = extract.calls[0]
    assert text
    assert session_id == received.correlation_id


def test_causation_id_is_incoming_event_id(received, s3, extract, producer):
    make_handler(s3, extract, producer)(received)

    _, event = producer.sent[0]
    assert event.causation_id == received.event_id
    assert event.event_id != received.event_id


def test_invoice_produces_no_event(s3, extract, producer):
    make_handler(s3, extract, producer)(classified_event("invoice"))

    assert producer.sent == []
    assert extract.calls == []


def test_missing_s3_object_is_permanent_error(received, extract, producer):
    handler = make_handler(FakeS3({}), extract, producer)

    with pytest.raises(PermanentError):
        handler(received)
    assert extract.calls == []
    assert producer.sent == []


# --- API ---


def test_health_returns_ok(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_extract_pdf_returns_purchase_order(client, extract):
    resp = client.post(
        "/extract",
        files={"file": ("PO-0001.pdf", PDF_PATH.read_bytes(), "application/pdf")},
    )

    assert resp.status_code == 200
    assert resp.json() == extract.po.model_dump(mode="json")
    assert extract.calls[0][1] == "manual-PO-0001.pdf"


def test_extract_non_pdf_returns_415(client, extract):
    resp = client.post(
        "/extract", files={"file": ("notes.txt", b"just some text", "text/plain")}
    )

    assert resp.status_code == 415
    assert extract.calls == []


# --- prompt ---


def test_load_prompt_inserts_json_schema():
    prompt = load_prompt(PROMPTS_DIR)

    assert "{schema}" not in prompt
    assert json.dumps(PurchaseOrder.model_json_schema()) in prompt
    assert prompt.startswith("You extract purchase orders")


# --- extractor ---


class FakeChatOpenAI:
    """Records the ChatOpenAI arguments; the structured-output chain returns a fixed PO."""

    instances: ClassVar[list["FakeChatOpenAI"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = None
        FakeChatOpenAI.instances.append(self)

    def with_structured_output(self, schema, method):
        assert (schema, method) == (PurchaseOrder, "json_mode")
        return self

    def invoke(self, messages):
        self.messages = messages
        return FakeExtract().po


@pytest.fixture
def fake_llm(monkeypatch):
    FakeChatOpenAI.instances = []
    monkeypatch.setattr(extractor.app, "ChatOpenAI", FakeChatOpenAI)
    return FakeChatOpenAI


def llm_settings() -> Settings:
    return Settings(
        s3_access_key="test",
        s3_secret_key="test",
        litellm_master_key="test-key",
        events_database_url="postgresql://test:test@localhost/test",
    )


def test_make_extractor_uses_o2c_extract_by_default(fake_llm):
    po = make_extractor(llm_settings())("PO text", "session-1")

    [llm] = fake_llm.instances
    assert llm.kwargs["model"] == "o2c-extract"
    assert llm.kwargs["api_key"] == "test-key"
    assert llm.kwargs["extra_body"]["metadata"]["session_id"] == "session-1"
    assert llm.messages == [("system", load_prompt(PROMPTS_DIR)), ("human", "PO text")]
    assert po == FakeExtract().po


def test_make_extractor_uses_the_given_model(fake_llm):
    make_extractor(llm_settings(), model="o2c-extract-candidate")(
        "PO text", "session-1"
    )

    assert fake_llm.instances[0].kwargs["model"] == "o2c-extract-candidate"
