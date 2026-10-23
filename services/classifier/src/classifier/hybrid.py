"""Hybrid document classification with risk-based routing (ADR-009).

DistilBERT (o2c-doc-classifier, trained by ml/train_classifier.py) is loaded once at
start: the model baked into the image (ADR-016) or, only if none exists,
models:/o2c-doc-classifier@champion from MLflow; restart the classifier to load a new
champion. Per document:
- DistilBERT says NOT purchase_order with confidence >= the non-PO threshold: accepted.
  A wrong non-PO label cannot create an order.
- DistilBERT says purchase_order: GPT decides. A false purchase order could create a wrong
  order, so it is always double-checked.
- DistilBERT says NOT purchase_order below the threshold: GPT decides.
- No model available: GPT decides every document.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from o2c_common.models import baked_model

log = logging.getLogger(__name__)

MODEL_NAME = "o2c-doc-classifier"
MODEL_URI = f"models:/{MODEL_NAME}@champion"
# Model input, as in training (ml/classifier_common.MAX_CHARS, ml/train_classifier.PARAMS).
MAX_CHARS = 1500
MAX_LENGTH = 256
PURCHASE_ORDER = "purchase_order"

# routing_reason values in the docs.classified payload
NON_PO_CONFIDENT = "non_po_confident"
PO_DOUBLE_CHECK = "po_double_check"
LOW_CONFIDENCE = "low_confidence"
MODEL_UNAVAILABLE = "model_unavailable"


@dataclass(frozen=True)
class HybridResult:
    doc_type: str
    reason: str
    classified_by: str  # "distilbert" or "gpt"
    confidence: float | None  # DistilBERT's softmax confidence; None if it did not run
    routing_reason: (
        str  # NON_PO_CONFIDENT, PO_DOUBLE_CHECK, LOW_CONFIDENCE, MODEL_UNAVAILABLE
    )


class DistilBert:
    """The fine-tuned DistilBERT: predict(text) -> (doc type, softmax confidence)."""

    def __init__(self, model, tokenizer):
        self.model = model.eval()
        self.tokenizer = tokenizer

    @classmethod
    def from_directory(cls, path: Path) -> "DistilBert":
        """A downloaded mlflow.transformers model: model/ and components/tokenizer/."""
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        return cls(
            AutoModelForSequenceClassification.from_pretrained(path / "model"),
            AutoTokenizer.from_pretrained(path / "components" / "tokenizer"),
        )

    def predict(self, text: str) -> tuple[str, float]:
        import torch

        encoded = self.tokenizer(
            text[:MAX_CHARS],
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
        )
        with torch.inference_mode():
            probs = self.model(**encoded).logits.softmax(dim=-1)[0]
        best = int(probs.argmax())
        return self.model.config.id2label[best], float(probs[best])


def download_model(tracking_uri: str) -> Path:
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    return Path(mlflow.artifacts.download_artifacts(MODEL_URI))


def load_distilbert(
    tracking_uri: str, download: Callable[[str], Path] = download_model
) -> DistilBert | None:
    """The baked model, else the champion from MLflow; None (with a warning) if neither
    loads. A baked model that fails to load does not fall back to MLflow."""
    baked = baked_model(MODEL_NAME)
    source = str(baked) if baked is not None else f"{MODEL_URI} from {tracking_uri}"
    try:
        model = DistilBert.from_directory(
            baked if baked is not None else download(tracking_uri)
        )
    except Exception as exc:  # noqa: BLE001 - without the model, GPT classifies everything
        log.warning(
            "document classifier %s unavailable, using GPT for every document: %s",
            source,
            exc,
        )
        return None
    log.info("loaded document classifier %s", source)
    return model


def route(doc_type: str, confidence: float, non_po_threshold: float) -> str:
    """Routing reason for a DistilBERT prediction; only NON_PO_CONFIDENT accepts it."""
    if doc_type == PURCHASE_ORDER:
        return PO_DOUBLE_CHECK
    if confidence >= non_po_threshold:
        return NON_PO_CONFIDENT
    return LOW_CONFIDENCE


def classify_hybrid(
    text: str,
    local: DistilBert | None,
    non_po_threshold: float,
    gpt: Callable[[str], tuple[str, str]],
) -> HybridResult:
    """DistilBERT for confident non-PO predictions, else gpt(text) -> (doc type, reason)."""
    if local is None:
        doc_type, reason = gpt(text)
        return HybridResult(doc_type, reason, "gpt", None, MODEL_UNAVAILABLE)
    predicted, confidence = local.predict(text)
    routing = route(predicted, confidence, non_po_threshold)
    if routing == NON_PO_CONFIDENT:
        return HybridResult(
            predicted,
            f"DistilBERT, p={confidence:.3f}",
            "distilbert",
            confidence,
            routing,
        )
    doc_type, reason = gpt(text)
    return HybridResult(doc_type, reason, "gpt", confidence, routing)
