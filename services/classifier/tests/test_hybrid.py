import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
from botocore.exceptions import ClientError
from classifier.app import (
    DEFAULT_NON_PO_THRESHOLD,
    REPO_ROOT,
    Settings,
    make_handler,
)
from classifier.hybrid import (
    MAX_CHARS,
    MAX_LENGTH,
    MODEL_URI,
    DistilBert,
    HybridResult,
    classify_hybrid,
    download_model,
    load_distilbert,
    route,
)
from o2c_common.events import Envelope
from o2c_common.pdf import pdf_text

PDF_PATH = REPO_ROOT / "poc" / "data" / "pos" / "PO-0001.pdf"
LABELS = {0: "purchase_order", 1: "invoice", 2: "remittance", 3: "other"}
GPT_RESPONSE = json.dumps({"doc_type": "invoice", "reason": "Recorded GPT answer."})


class FakeLocal:
    """DistilBERT stand-in with a fixed prediction."""

    def __init__(self, doc_type: str, confidence: float):
        self.result = (doc_type, confidence)
        self.texts: list[str] = []

    def predict(self, text: str) -> tuple[str, float]:
        self.texts.append(text)
        return self.result


class Gpt:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, text: str) -> tuple[str, str]:
        self.texts.append(text)
        return "invoice", "GPT says so"


class FakeLLM:
    """OpenAI client with a recorded chat completion."""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(role="assistant", content=GPT_RESPONSE)
        return SimpleNamespace(choices=[SimpleNamespace(index=0, message=message)])


class FakeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class FakeProducer:
    def __init__(self):
        self.sent: list[tuple[str, Envelope]] = []

    def send(self, topic, event):
        self.sent.append((topic, event))


# --- classify_hybrid: risk-based routing ---


def test_confident_non_po_uses_distilbert_only():
    gpt = Gpt()

    result = classify_hybrid("text", FakeLocal("remittance", 0.97), 0.9, gpt)

    assert result == HybridResult(
        "remittance", "DistilBERT, p=0.970", "distilbert", 0.97, "non_po_confident"
    )
    assert gpt.texts == []


def test_confidence_equal_to_threshold_is_accepted():
    result = classify_hybrid("text", FakeLocal("other", 0.9), 0.9, Gpt())

    assert result.classified_by == "distilbert"
    assert result.routing_reason == "non_po_confident"


def test_purchase_order_is_always_double_checked_by_gpt():
    gpt = Gpt()

    result = classify_hybrid("text", FakeLocal("purchase_order", 0.999), 0.9, gpt)

    # GPT's answer wins, even against a very confident DistilBERT
    assert result == HybridResult(
        "invoice", "GPT says so", "gpt", 0.999, "po_double_check"
    )
    assert gpt.texts == ["text"]


def test_low_confidence_non_po_calls_gpt():
    gpt = Gpt()

    result = classify_hybrid("text", FakeLocal("other", 0.6), 0.9, gpt)

    assert result == HybridResult(
        "invoice", "GPT says so", "gpt", 0.6, "low_confidence"
    )
    assert gpt.texts == ["text"]


def test_without_model_gpt_classifies_everything():
    gpt = Gpt()

    result = classify_hybrid("text", None, 0.9, gpt)

    assert result == HybridResult(
        "invoice", "GPT says so", "gpt", None, "model_unavailable"
    )
    assert gpt.texts == ["text"]


@pytest.mark.parametrize(
    ("doc_type", "confidence", "expected"),
    [
        ("invoice", 0.95, "non_po_confident"),
        ("remittance", 0.5, "low_confidence"),
        ("purchase_order", 1.0, "po_double_check"),
        ("purchase_order", 0.3, "po_double_check"),
    ],
)
def test_route(doc_type, confidence, expected):
    assert route(doc_type, confidence, 0.9) == expected


# --- DistilBert ---


class FakeTokenizer:
    def __init__(self):
        self.calls = []

    def __call__(self, text, truncation, max_length, return_tensors):
        self.calls.append((text, truncation, max_length, return_tensors))
        return {"input_ids": torch.tensor([[1, 2, 3]])}


class FakeModel:
    def __init__(self, logits: list[float]):
        self.logits = torch.tensor([logits])
        self.config = SimpleNamespace(id2label=LABELS)
        self.evaluated = False

    def eval(self):
        self.evaluated = True
        return self

    def __call__(self, input_ids):
        return SimpleNamespace(logits=self.logits)


def test_distilbert_predicts_label_and_softmax_confidence():
    tokenizer = FakeTokenizer()
    model = FakeModel([0.0, 3.0, 0.0, 0.0])

    doc_type, confidence = DistilBert(model, tokenizer).predict("x" * (MAX_CHARS + 500))

    expected = torch.tensor([0.0, 3.0, 0.0, 0.0]).softmax(dim=-1)[1].item()
    assert doc_type == "invoice"
    assert confidence == pytest.approx(expected)
    assert model.evaluated
    text, truncation, max_length, tensors = tokenizer.calls[0]
    assert len(text) == MAX_CHARS  # the input the model was trained on
    assert (truncation, max_length, tensors) == (True, MAX_LENGTH, "pt")


def test_from_directory_reads_model_and_tokenizer(monkeypatch, tmp_path):
    import transformers

    loaded = {}

    def fake(kind, result):
        def from_pretrained(path):
            loaded[kind] = Path(path)
            return result

        return SimpleNamespace(from_pretrained=from_pretrained)

    model = FakeModel([1.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(
        transformers, "AutoModelForSequenceClassification", fake("model", model)
    )
    monkeypatch.setattr(transformers, "AutoTokenizer", fake("tokenizer", "tok"))

    local = DistilBert.from_directory(tmp_path)

    assert loaded == {
        "model": tmp_path / "model",
        "tokenizer": tmp_path / "components" / "tokenizer",
    }
    assert local.model is model and local.tokenizer == "tok"


def test_download_model_uses_tracking_uri_and_champion(monkeypatch, tmp_path):
    import mlflow

    calls = []
    monkeypatch.setattr(mlflow, "set_tracking_uri", calls.append)
    monkeypatch.setattr(
        mlflow.artifacts,
        "download_artifacts",
        lambda uri: calls.append(uri) or str(tmp_path),
    )

    assert download_model("http://mlflow:5000") == tmp_path
    assert calls == ["http://mlflow:5000", "models:/o2c-doc-classifier@champion"]
    assert MODEL_URI == "models:/o2c-doc-classifier@champion"


def test_load_distilbert_returns_none_and_warns_when_unavailable(caplog):
    def unreachable(uri):
        raise ConnectionError("mlflow down")

    with caplog.at_level(logging.WARNING):
        assert load_distilbert("http://mlflow:5000", download=unreachable) is None
    assert "using GPT for every document" in caplog.text
    assert "mlflow down" in caplog.text


def test_load_distilbert_loads_the_downloaded_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        DistilBert, "from_directory", classmethod(lambda cls, path: ("model", path))
    )

    assert load_distilbert("uri", download=lambda uri: tmp_path) == ("model", tmp_path)


# --- handler ---


@pytest.fixture
def received() -> Envelope:
    return Envelope.new(
        "docs.received",
        "intake",
        "b" * 32,
        {"s3_bucket": "o2c-documents", "s3_key": "incoming/po.pdf"},
    )


@pytest.fixture
def s3(received) -> FakeS3:
    p = received.payload
    return FakeS3({(p["s3_bucket"], p["s3_key"]): PDF_PATH.read_bytes()})


def test_handler_confident_non_po_skips_gpt(received, s3):
    llm, producer = FakeLLM(), FakeProducer()
    local = FakeLocal("remittance", 0.99)

    make_handler(s3, llm, producer, "prompt", local=local, non_po_threshold=0.9)(
        received
    )

    assert llm.calls == []
    assert local.texts == [pdf_text(PDF_PATH.read_bytes())]
    [(topic, event)] = producer.sent
    assert topic == "docs.classified"
    assert event.payload == {
        **received.payload,
        "doc_type": "remittance",
        "reason": "DistilBERT, p=0.990",
        "classified_by": "distilbert",
        "confidence": 0.99,
        "routing_reason": "non_po_confident",
    }


def test_handler_purchase_order_is_double_checked(received, s3):
    llm, producer = FakeLLM(), FakeProducer()
    local = FakeLocal("purchase_order", 0.99)

    make_handler(s3, llm, producer, "prompt", local=local, non_po_threshold=0.9)(
        received
    )

    assert len(llm.calls) == 1
    [(_, event)] = producer.sent
    assert event.payload["doc_type"] == "invoice"  # GPT's answer
    assert event.payload["reason"] == "Recorded GPT answer."
    assert event.payload["classified_by"] == "gpt"
    assert event.payload["confidence"] == 0.99
    assert event.payload["routing_reason"] == "po_double_check"


def test_handler_low_confidence_asks_gpt(received, s3):
    llm, producer = FakeLLM(), FakeProducer()
    local = FakeLocal("other", 0.5)

    make_handler(s3, llm, producer, "prompt", local=local, non_po_threshold=0.9)(
        received
    )

    assert len(llm.calls) == 1
    [(_, event)] = producer.sent
    assert event.payload["classified_by"] == "gpt"
    assert event.payload["routing_reason"] == "low_confidence"


def test_handler_without_model_uses_gpt_only(received, s3):
    llm, producer = FakeLLM(), FakeProducer()

    make_handler(s3, llm, producer, "prompt", local=None)(received)

    assert len(llm.calls) == 1
    [(_, event)] = producer.sent
    assert event.payload["classified_by"] == "gpt"
    assert event.payload["confidence"] is None
    assert event.payload["routing_reason"] == "model_unavailable"


# --- settings ---


def make_settings() -> Settings:
    return Settings(
        s3_access_key="t",
        s3_secret_key="t",
        litellm_master_key="t",
        events_database_url="t",
        _env_file=None,
    )


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("CLASSIFIER_NON_PO_THRESHOLD", raising=False)

    settings = make_settings()

    assert settings.mlflow_tracking_uri == "http://localhost:5000"
    assert settings.classifier_non_po_threshold == DEFAULT_NON_PO_THRESHOLD == 0.005


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_NON_PO_THRESHOLD", "0.9")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")

    settings = make_settings()

    assert settings.classifier_non_po_threshold == 0.9
    assert settings.mlflow_tracking_uri == "http://mlflow:5000"
