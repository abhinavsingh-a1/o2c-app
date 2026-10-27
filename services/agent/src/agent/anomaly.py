"""Quantity anomaly score for one order line: 0.0 = normal, 1.0 = very unusual.

The model (ml/train_anomaly.py) is loaded once: the model baked into the image (ADR-016)
or, only if none exists, models:/o2c-quantity-anomaly@champion from MLflow
(MLFLOW_TRACKING_URI, default http://localhost:5000). If the model is unavailable or its
prediction fails, a warning is logged and the score is 0.0: the agent never fails because
of the anomaly model. Every such line counts in o2c_anomaly_scoring_failures_total (alert
AnomalyScoringFailing), because a 0.0 fallback silently turns anomaly detection off
(ADR-006). After a failed load, loading is retried at most every RETRY_SECONDS, so the
model is picked up once MLflow is reachable.
"""

import logging
import os
import threading
import time

import pandas as pd
from o2c_common.metrics import ANOMALY_SCORING_FAILURES
from o2c_common.models import baked_model

log = logging.getLogger(__name__)

MODEL_NAME = "o2c-quantity-anomaly"
MODEL_URI = f"models:/{MODEL_NAME}@champion"
DEFAULT_TRACKING_URI = "http://localhost:5000"
RETRY_SECONDS = 300.0

_lock = threading.Lock()
_model = None
_failed_at: float | None = None


def _load():
    import mlflow  # imported here so that even a broken MLflow install only disables scoring

    baked = baked_model(MODEL_NAME)
    if baked is not None:
        log.info("loading anomaly model from %s", baked)
        return mlflow.pyfunc.load_model(str(baked))
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_TRACKING_URI))
    return mlflow.pyfunc.load_model(MODEL_URI)


def get_model():
    """The loaded model, or None while MLflow or the model is unavailable."""
    global _model, _failed_at
    with _lock:
        if _model is None and (
            _failed_at is None or time.monotonic() - _failed_at >= RETRY_SECONDS
        ):
            try:
                _model = _load()
                _failed_at = None
                log.info("loaded anomaly model %s", MODEL_URI)
            except Exception as exc:  # noqa: BLE001 - scoring is optional, the agent is not
                _failed_at = time.monotonic()
                log.warning(
                    "anomaly model %s unavailable, scoring 0.0: %s", MODEL_URI, exc
                )
        return _model


def reset() -> None:
    """Forget the loaded model (tests, or to pick up a new champion)."""
    global _model, _failed_at
    with _lock:
        _model, _failed_at = None, None


def score_line(model, customer_number: str, matnr: str, quantity: float) -> float:
    """Score of one order line with a loaded pyfunc model; raises if prediction fails
    (the eval gate scores the champion through this, without the fallback)."""
    line = pd.DataFrame(
        [
            {
                "customer_number": customer_number,
                "material_number": matnr,
                "quantity": float(quantity),
            }
        ]
    )
    return float(model.predict(line)[0])


def anomaly_score(customer_number: str, matnr: str, quantity: float) -> float:
    model = get_model()
    if model is None:
        ANOMALY_SCORING_FAILURES.labels("model_unavailable").inc()
        return 0.0
    try:
        return score_line(model, customer_number, matnr, quantity)
    except Exception as exc:  # noqa: BLE001
        ANOMALY_SCORING_FAILURES.labels("prediction_error").inc()
        log.warning(
            "anomaly scoring failed for %s/%s, scoring 0.0: %s",
            customer_number,
            matnr,
            exc,
        )
        return 0.0
