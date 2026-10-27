"""Quantity anomaly model and the agent's scoring, without an MLflow server."""

import logging

import mlflow
import numpy as np
import pandas as pd
import pytest
from agent import anomaly
from agent.anomaly_model import (
    FEATURES,
    THRESHOLD,
    best_cutoff,
    calibrate,
    features,
    history_stats,
    raw_scores,
    train,
)
from agent.graph import ANOMALY_THRESHOLD
from mlflow.models import infer_signature
from o2c_common.metrics import sample

REAL_LOAD = anomaly._load  # conftest replaces it with an unavailable loader

CUSTOMERS = [f"10001{i:02d}" for i in range(1, 7)]
MATERIALS = [f"4001{i:02d}" for i in range(1, 26)]


def synthetic_history(seed: int = 7) -> tuple[pd.DataFrame, np.ndarray, dict]:
    """A small order history: each customer buys 6-10 materials with a typical
    quantity; a few lines are 5-20x outliers or a material the customer never buys."""
    rng = np.random.default_rng(seed)
    rows, labels, means = [], [], {}
    for customer in CUSTOMERS:
        regular = rng.choice(MATERIALS, rng.integers(6, 11), replace=False)
        for material in regular:
            mean = float(rng.uniform(5, 150))
            means[(customer, material)] = mean
            for _ in range(rng.integers(15, 40)):
                rows.append(
                    (customer, material, max(1.0, round(rng.normal(mean, mean * 0.25))))
                )
                labels.append(False)
        for material in regular[:2]:  # quantity outliers
            rows.append(
                (
                    customer,
                    material,
                    round(means[(customer, material)] * rng.uniform(5, 20)),
                )
            )
            labels.append(True)
        never = next(m for m in MATERIALS if m not in regular)
        rows.append((customer, never, float(rng.integers(1, 20))))
        labels.append(True)
    history = pd.DataFrame(
        rows, columns=["customer_number", "material_number", "quantity"]
    )
    return history, np.array(labels), means


@pytest.fixture(scope="module")
def trained():
    history, labels, means = synthetic_history()
    model, metrics = train(history, labels)
    return model, metrics, means


def lines(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["customer_number", "material_number", "quantity"]
    )


# --- features and calibration ----------------------------------------------


def test_threshold_is_the_graph_threshold():
    assert THRESHOLD == ANOMALY_THRESHOLD == 0.8


def test_history_stats_and_features():
    history = lines(
        ("C1", "M1", 10), ("C1", "M1", 12), ("C1", "M1", 14), ("C1", "M2", 3)
    )
    stats = history_stats(history)
    assert stats[("C1", "M1")] == {"count": 3.0, "median": 12.0, "mad": 2.0}

    x = features(lines(("C1", "M1", 24), ("C1", "M3", 5)), stats)
    assert x.shape == (2, len(FEATURES))
    log_q, ratio, z, never = x[0]
    assert ratio == 2.0 and z == pytest.approx(0.6745 * 12 / 2.0) and never == 0.0
    assert log_q == pytest.approx(np.log1p(24))
    assert list(x[1][1:]) == [1.0, 0.0, 1.0]  # no history: ratio 1, z 0, never bought


def test_never_bought_leaves_the_line_itself_out_in_training():
    history = lines(("C1", "M1", 10), ("C1", "M1", 11), ("C1", "M2", 3))
    x = features(history, history_stats(history), in_history=True)
    assert list(x[:, 3]) == [0.0, 0.0, 1.0]  # M2 was bought once: only by this line


def test_mad_floor_avoids_division_by_zero():
    history = lines(*[("C1", "M1", 10)] * 5)  # MAD 0
    _, _, z, _ = features(lines(("C1", "M1", 11)), history_stats(history))[0]
    assert np.isfinite(z) and z == pytest.approx(
        0.6745 * 1 / 1.0
    )  # MAD floored to 10% of 10


def test_best_cutoff_and_calibration():
    raw = np.array([0.1, 0.2, 0.3, 0.6, 0.7, 0.9])
    actual = np.array([False, False, False, True, False, True])

    cutoff, metrics = best_cutoff(raw, actual)

    assert cutoff == pytest.approx(0.45)  # flags 0.6, 0.7, 0.9: F1 0.8
    assert metrics["f1"] == pytest.approx(0.8)
    calibration = calibrate(raw, cutoff)
    assert calibration.score(np.array([cutoff]))[0] == pytest.approx(THRESHOLD)
    assert list(calibration.score(raw) > THRESHOLD) == list(raw > cutoff)


# --- trained model ----------------------------------------------------------


def test_training_finds_the_labelled_outliers(trained):
    _, metrics, _ = trained
    assert metrics["f1"] >= 0.8
    assert metrics["outliers"] == 3 * len(CUSTOMERS)


def test_scores_are_between_0_and_1(trained):
    model, _, means = trained
    (customer, material), mean = next(iter(means.items()))
    scores = model.score(
        lines(
            (customer, material, mean),
            (customer, material, 0),
            (customer, material, mean * 1000),
            ("9999999", "499999", 1),
        )
    )
    assert ((scores >= 0) & (scores <= 1)).all()


def test_normal_quantity_scores_below_threshold(trained):
    model, _, means = trained
    normal = lines(*[(c, m, round(mean)) for (c, m), mean in means.items()])
    assert (model.score(normal) < THRESHOLD).mean() >= 0.95


def test_20x_quantity_scores_above_threshold(trained):
    model, _, means = trained
    unusual = lines(*[(c, m, round(mean * 20)) for (c, m), mean in means.items()])
    assert (model.score(unusual) > THRESHOLD).all()


def test_raw_scores_increase_with_the_quantity(trained):
    model, _, means = trained
    (customer, material), mean = next(iter(means.items()))
    x = features(
        lines(*[(customer, material, mean * k) for k in (1, 5, 20)]), model.stats
    )
    raw = raw_scores(model.forest, x)
    assert raw[0] < raw[1] <= raw[2]


# --- agent.anomaly: loading from MLflow and fallback ------------------------


def failures(reason: str) -> float:
    return sample("o2c_anomaly_scoring_failures_total", reason=reason)


def test_pandas_is_the_major_version_mlflow_2_17_supports():
    # MLflow 2.17's schema enforcement rejects pandas 3 string columns ("Can not safely
    # convert str to <U0"): every line would get the 0.0 fallback (ADR-006).
    assert mlflow.__version__.startswith("2.17.")
    assert pd.__version__.startswith("2."), pd.__version__


def test_a_signed_pyfunc_model_scores_with_the_locked_pandas(
    trained, tmp_path, monkeypatch
):
    # Saved as ml/train_anomaly.register logs it: with signature and input example, so
    # MLflow enforces the input schema on every predict, as for the registered model.
    model, _, means = trained
    (customer, material), mean = max(means.items(), key=lambda kv: kv[1])
    example = lines((customer, material, float(round(mean))), (customer, material, 1.0))
    mlflow.pyfunc.save_model(
        path=tmp_path / "model",
        python_model=model,
        input_example=example,
        signature=infer_signature(example, model.score(example)),
    )
    loaded = mlflow.pyfunc.load_model(str(tmp_path / "model"))
    assert loaded.metadata.get_input_schema().input_names() == [
        "customer_number",
        "material_number",
        "quantity",
    ]
    monkeypatch.setattr(anomaly, "_load", lambda: loaded)
    before = failures("model_unavailable"), failures("prediction_error")

    normal = anomaly.anomaly_score(customer, material, round(mean))
    unusual = anomaly.anomaly_score(customer, material, round(mean * 20))

    assert 0.0 < normal < THRESHOLD < unusual <= 1.0  # 0.0 would be the fallback
    assert (failures("model_unavailable"), failures("prediction_error")) == before


def test_anomaly_score_with_a_saved_pyfunc_model(trained, tmp_path, monkeypatch):
    model, _, means = trained
    mlflow.pyfunc.save_model(path=tmp_path / "model", python_model=model)
    loaded = mlflow.pyfunc.load_model(str(tmp_path / "model"))
    monkeypatch.setattr(anomaly, "_load", lambda: loaded)
    (customer, material), mean = max(means.items(), key=lambda kv: kv[1])

    normal = anomaly.anomaly_score(customer, material, round(mean))
    unusual = anomaly.anomaly_score(customer, material, round(mean * 20))

    assert 0.0 <= normal < THRESHOLD < unusual <= 1.0


def test_fallback_returns_0_when_mlflow_is_unavailable(monkeypatch, caplog):
    calls = []

    def unavailable():
        calls.append(1)
        raise ConnectionError("mlflow:5000 refused")

    monkeypatch.setattr(anomaly, "_load", unavailable)
    before = failures("model_unavailable")

    with caplog.at_level(logging.WARNING, logger="agent.anomaly"):
        assert anomaly.anomaly_score("1000107", "400101", 10_000) == 0.0
        assert anomaly.anomaly_score("1000107", "400101", 10_000) == 0.0

    assert len(calls) == 1  # no reload on every line
    assert failures("model_unavailable") - before == 2  # but every line is counted
    assert "unavailable" in caplog.text


def test_fallback_retries_after_the_retry_interval(trained, monkeypatch):
    model, _, _ = trained
    clock = [1000.0]
    monkeypatch.setattr(anomaly.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        anomaly, "_load", lambda: (_ for _ in ()).throw(OSError("down"))
    )
    assert anomaly.get_model() is None

    loaded = type("Loaded", (), {"predict": lambda self, df: model.score(df)})()
    monkeypatch.setattr(anomaly, "_load", lambda: loaded)
    clock[0] += anomaly.RETRY_SECONDS - 1
    assert anomaly.get_model() is None  # still waiting
    clock[0] += 1
    assert anomaly.get_model() is loaded


def test_fallback_when_the_model_is_not_registered(tmp_path, monkeypatch, caplog):
    # A real MLflow registry without the model: loading fails fast, scoring returns 0.0.
    monkeypatch.setattr(anomaly, "_load", REAL_LOAD)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tmp_path.as_uri())
    with caplog.at_level(logging.WARNING, logger="agent.anomaly"):
        assert anomaly.anomaly_score("1000107", "400101", 25) == 0.0
    assert "o2c-quantity-anomaly@champion" in caplog.text


def test_the_baked_model_is_loaded_without_mlflow(trained, tmp_path, monkeypatch):
    model, _, means = trained
    models_dir = tmp_path / "models"
    mlflow.pyfunc.save_model(
        path=models_dir / "o2c-quantity-anomaly", python_model=model
    )
    monkeypatch.setenv("O2C_MODELS_DIR", str(models_dir))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://unreachable.invalid:5000")
    monkeypatch.setattr(anomaly, "_load", REAL_LOAD)
    (customer, material), mean = max(means.items(), key=lambda kv: kv[1])

    assert anomaly.anomaly_score(customer, material, round(mean * 20)) > THRESHOLD


def test_fallback_when_prediction_fails(monkeypatch, caplog):
    class Broken:
        def predict(self, df):
            raise ValueError("schema mismatch")

    monkeypatch.setattr(anomaly, "_load", Broken)
    before = failures("prediction_error")
    with caplog.at_level(logging.WARNING, logger="agent.anomaly"):
        assert anomaly.anomaly_score("1000107", "400101", 25) == 0.0
    assert "scoring failed" in caplog.text
    assert failures("prediction_error") - before == 1


def test_score_line_raises_instead_of_falling_back():
    class Broken:
        def predict(self, df):
            assert list(df.columns) == [
                "customer_number",
                "material_number",
                "quantity",
            ]
            raise ValueError("schema mismatch")

    with pytest.raises(ValueError, match="schema mismatch"):
        anomaly.score_line(Broken(), "1000107", "400101", 25)
