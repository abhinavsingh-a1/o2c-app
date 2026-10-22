import io
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from classifier.app import (
    REPO_ROOT,
    Classification,
    classify,
    load_prompt,
    make_handler,
)
from o2c_common.events import Envelope
from o2c_common.kafka import PermanentError

PDF_PATH = REPO_ROOT / "poc" / "data" / "pos" / "PO-0001.pdf"
PROMPTS_DIR = REPO_ROOT / "prompts"
SYSTEM_PROMPT = "classify this"

PO_RESPONSE = json.dumps(
    {"doc_type": "purchase_order", "reason": "Customer orders goods from Alpentech."}
)


def recorded_response(content: str) -> SimpleNamespace:
    """Shape of an OpenAI chat completion, as far as classify() reads it."""
    message = SimpleNamespace(role="assistant", content=content)
    return SimpleNamespace(choices=[SimpleNamespace(index=0, message=message)])


class FakeLLM:
    def __init__(self, content: str):
        self.calls: list[dict] = []
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return recorded_response(self._content)


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


@pytest.fixture
def received() -> Envelope:
    return Envelope.new(
        "docs.received",
        "intake",
        "a" * 32,
        {
            "s3_bucket": "o2c-documents",
            "s3_key": "incoming/abc.pdf",
            "filename": "PO-0001.pdf",
            "source": "email",
            "sender": "buyer@example.com",
            "size_bytes": 1234,
        },
    )


@pytest.fixture
def s3(received) -> FakeS3:
    p = received.payload
    return FakeS3({(p["s3_bucket"], p["s3_key"]): PDF_PATH.read_bytes()})


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


def test_purchase_order_emits_classified_child_event(received, s3, producer):
    llm = FakeLLM(PO_RESPONSE)

    make_handler(s3, llm, producer, SYSTEM_PROMPT)(received)

    assert len(producer.sent) == 1
    topic, event = producer.sent[0]
    assert topic == "docs.classified"
    assert event.event_type == "docs.classified"
    assert event.producer == "classifier"
    assert event.payload == {
        **received.payload,
        "doc_type": "purchase_order",
        "reason": "Customer orders goods from Alpentech.",
        "classified_by": "gpt",  # no DistilBERT model given
        "confidence": None,
        "routing_reason": "model_unavailable",
    }
    assert event.idempotency_key == received.idempotency_key
    assert event.correlation_id == received.correlation_id

    messages = llm.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1]["role"] == "user"
    assert 0 < len(messages[1]["content"]) <= 4000


def test_causation_id_is_incoming_event_id(received, s3, producer):
    make_handler(s3, FakeLLM(PO_RESPONSE), producer, SYSTEM_PROMPT)(received)

    _, event = producer.sent[0]
    assert event.causation_id == received.event_id
    assert event.event_id != received.event_id


def test_missing_s3_object_is_permanent_error(received, producer):
    llm = FakeLLM(PO_RESPONSE)
    handler = make_handler(FakeS3({}), llm, producer, SYSTEM_PROMPT)

    with pytest.raises(PermanentError):
        handler(received)
    assert llm.calls == []
    assert producer.sent == []


def test_invalid_llm_json_raises_so_consumer_retries(received, s3, producer):
    handler = make_handler(s3, FakeLLM("not json {"), producer, SYSTEM_PROMPT)

    with pytest.raises(Exception) as exc_info:
        handler(received)
    assert not isinstance(exc_info.value, PermanentError)
    assert producer.sent == []


def test_load_prompt_describes_the_fields_without_the_raw_schema():
    prompt = load_prompt(PROMPTS_DIR)

    assert "{schema}" not in prompt
    assert '"properties"' not in prompt
    assert '"doc_type"' in prompt and '"reason"' in prompt
    assert prompt.startswith("You classify documents")


def test_classify_uses_strict_structured_outputs(received):
    llm = FakeLLM(PO_RESPONSE)

    classify(llm, SYSTEM_PROMPT, "text", received)

    fmt = llm.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == Classification.model_json_schema()


def test_classify_rejects_an_answer_wrapped_in_properties(received):
    wrapped = '{"properties": {"doc_type": "purchase_order", "reason": "x"}}'

    with pytest.raises(ValueError):
        classify(FakeLLM(wrapped), SYSTEM_PROMPT, "text", received)
