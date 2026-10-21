"""Quantity anomaly score for one order line: 0.0 = normal, 1.0 = very unusual.

The model (ml/train_anomaly.py) is loaded once from MLflow as
models:/o2c-quantity-anomaly@champion (MLFLOW_TRACKING_URI, default http://localhost:5000).
If MLflow or the model is unavailable, a warning is logged and the score is 0.0: the agent
never fails because of the anomaly model. After a failed load, loading is retried at most
every RETRY_SECONDS, so the model is picked up once MLflow is reachable.
"""

import logging
import os
import threading
import time

import pandas as pd

log = logging.getLogger(__name__)

MODEL_URI = "models:/o2c-quantity-anomaly@champion"
DEFAULT_TRACKING_URI = "http://localhost:5000"
RETRY_SECONDS = 300.0

_lock = threading.Lock()
_model = None
_failed_at: float | None = None


def _load():
    import mlflow  # imported here so that even a broken MLflow install only disables scoring

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


def anomaly_score(customer_number: str, matnr: str, quantity: float) -> float:
    model = get_model()
    if model is None:
        return 0.0
    try:
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
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "anomaly scoring failed for %s/%s, scoring 0.0: %s",
            customer_number,
            matnr,
            exc,
        )
        return 0.0
