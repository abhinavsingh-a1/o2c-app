import base64
import json

import pytest
from intake.app import MAX_PDF_BYTES, Settings, create_app, sign

SECRET = "test-secret"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeS3:
    def __init__(self):
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


class FakeProducer:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self.flushes = 0

    def send(self, topic, event):
        self.sent.append((topic, event))

    def flush(self, timeout: float = 10.0):
        self.flushes += 1


@pytest.fixture
def s3():
    return FakeS3()


@pytest.fixture
def producer():
    return FakeProducer()


@pytest.fixture
def client(s3, producer):
    settings = Settings(
        intake_hmac_secret=SECRET,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="test-bucket",
    )
    return create_app(settings, s3=s3, producer=producer).test_client()


def make_body(content: bytes = PDF, filename: str = "po.pdf", **overrides) -> bytes:
    data = {
        "source": "email",
        "filename": filename,
        "sender": "buyer@example.com",
        "content_base64": base64.b64encode(content).decode(),
    }
    data.update(overrides)
    return json.dumps(data).encode()


def post(client, body: bytes, signature: str | None = None):
    return client.post(
        "/intake",
        data=body,
        content_type="application/json",
        headers={
            "X-O2C-Signature": signature
            if signature is not None
            else sign(SECRET, body)
        },
    )


def assert_nothing_stored(s3, producer):
    assert s3.puts == []
    assert producer.sent == []


def test_valid_pdf_is_stored_and_published(client, s3, producer):
    resp = post(client, make_body())

    assert resp.status_code == 202
    key = resp.get_json()["idempotency_key"]

    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "test-bucket"
    assert put["Key"] == f"incoming/{key}.pdf"
    assert put["Body"] == PDF
    assert put["ContentType"] == "application/pdf"

    assert len(producer.sent) == 1
    topic, event = producer.sent[0]
    assert topic == "docs.received"
    assert event.event_type == "docs.received"
    assert str(event.event_id) == resp.get_json()["event_id"]
    assert event.idempotency_key == key
    assert event.payload["s3_key"] == put["Key"]
    assert event.payload["size_bytes"] == len(PDF)
    assert producer.flushes == 1


def test_same_pdf_twice_gives_same_event_id(client, producer):
    first = post(client, make_body())
    second = post(client, make_body())

    assert first.status_code == second.status_code == 202
    assert first.get_json()["event_id"] == second.get_json()["event_id"]
    assert producer.sent[0][1].event_id == producer.sent[1][1].event_id


def test_wrong_signature_is_rejected(client, s3, producer):
    resp = post(client, make_body(), signature="0" * 64)

    assert resp.status_code == 401
    assert_nothing_stored(s3, producer)


def test_invalid_json_is_rejected(client, s3, producer):
    resp = post(client, b"{not json")

    assert resp.status_code == 400
    assert_nothing_stored(s3, producer)


def test_non_pdf_filename_is_rejected(client, s3, producer):
    resp = post(client, make_body(filename="po.docx"))

    assert resp.status_code == 415
    assert_nothing_stored(s3, producer)


def test_content_without_pdf_magic_is_rejected(client, s3, producer):
    resp = post(client, make_body(content=b"PK\x03\x04 not a pdf"))

    assert resp.status_code == 415
    assert_nothing_stored(s3, producer)


def test_invalid_base64_is_rejected(client, s3, producer):
    resp = post(client, make_body(content_base64="not base64!!"))

    assert resp.status_code == 400
    assert_nothing_stored(s3, producer)


def test_file_over_10_mb_is_rejected(client, s3, producer):
    big = b"%PDF-" + b"0" * (MAX_PDF_BYTES - 4)

    resp = post(client, make_body(content=big))

    assert resp.status_code == 413
    assert_nothing_stored(s3, producer)
