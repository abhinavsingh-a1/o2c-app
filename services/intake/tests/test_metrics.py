import base64
import json

import pytest
from intake.app import Settings, create_app, sign
from o2c_common.metrics import sample

SECRET = "test-secret"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeS3:
    def put_object(self, **kwargs):
        pass


class FakeProducer:
    def send(self, topic, event):
        pass

    def flush(self, timeout: float = 10.0):
        pass


@pytest.fixture
def client():
    settings = Settings(intake_hmac_secret=SECRET, s3_access_key="t", s3_secret_key="t")
    return create_app(settings, s3=FakeS3(), producer=FakeProducer()).test_client()


def post(client, source: str, signature: str | None = None):
    body = json.dumps(
        {
            "source": source,
            "filename": "po.pdf",
            "content_base64": base64.b64encode(PDF).decode(),
        }
    ).encode()
    return client.post(
        "/intake",
        data=body,
        content_type="application/json",
        headers={"X-O2C-Signature": signature or sign(SECRET, body)},
    )


def received(source: str) -> float:
    return sample("o2c_documents_received_total", source=source)


def requests(status: str) -> float:
    return sample(
        "o2c_http_requests_total", method="POST", route="/intake", status=status
    )


def test_accepted_documents_are_counted_per_source(client):
    email, upload, accepted = received("email"), received("upload"), requests("202")

    post(client, "email")
    post(client, "upload")
    post(client, "upload")

    assert received("email") - email == 1
    assert received("upload") - upload == 2
    assert requests("202") - accepted == 3


def test_rejected_documents_are_not_received(client):
    email, rejected = received("email"), requests("401")

    assert post(client, "email", signature="wrong").status_code == 401

    assert received("email") == email
    assert requests("401") - rejected == 1


def test_metrics_endpoint(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert b"o2c_documents_received_total" in response.data
