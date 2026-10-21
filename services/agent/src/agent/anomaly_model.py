"""Quantity anomaly model: features, IsolationForest training, score calibration and the
MLflow pyfunc wrapper. Shared by ml/train_anomaly.py (training) and agent.anomaly (scoring),
so both compute exactly the same features.

Order lines are DataFrames with the columns customer_number, material_number, quantity.
"""

import math
from dataclasses import dataclass

import mlflow.pyfunc
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

FEATURES = ["log_quantity", "median_ratio", "robust_z", "never_bought"]
LINE_COLUMNS = ["customer_number", "material_number", "quantity"]
THRESHOLD = 0.8  # graph.ANOMALY_THRESHOLD: a score above it raises QUANTITY_ANOMALY
RANDOM_STATE = 2026
N_ESTIMATORS = 200
MIN_MAD_SHARE = (
    0.1  # MAD floor as a share of the median, so stable buyers do not divide by ~0
)
MAD_TO_SIGMA = 0.6745  # robust z = 0.6745 * (x - median) / MAD

Stats = dict[tuple[str, str], dict[str, float]]


def history_stats(history: pd.DataFrame) -> Stats:
    """Per (customer, material): number of order lines, median quantity and MAD."""
    stats: Stats = {}
    for (customer, material), group in history.groupby(
        ["customer_number", "material_number"]
    ):
        quantities = group["quantity"].astype(float).to_numpy()
        median = float(np.median(quantities))
        stats[(customer, material)] = {
            "count": float(len(quantities)),
            "median": median,
            "mad": float(np.median(np.abs(quantities - median))),
        }
    return stats


def features(lines: pd.DataFrame, stats: Stats, in_history: bool = False) -> np.ndarray:
    """Feature matrix (columns FEATURES) for order lines.

    `in_history`: the lines are part of `stats` (training), so each line is left out of
    its own purchase count; "never bought" means no *other* purchase of that material.
    Without history for a customer-material, the ratio is 1 and the z-score 0; the
    never_bought flag carries the signal.
    """
    rows = []
    for customer, material, quantity in lines[LINE_COLUMNS].itertuples(index=False):
        q = float(quantity)
        s = stats.get((customer, material))
        others = (s["count"] - (1 if in_history else 0)) if s else 0
        if s and s["median"] > 0:
            mad = max(s["mad"], MIN_MAD_SHARE * s["median"], 1e-9)
            ratio, z = q / s["median"], MAD_TO_SIGMA * (q - s["median"]) / mad
        else:
            ratio, z = 1.0, 0.0
        rows.append([math.log1p(max(q, 0.0)), ratio, z, 1.0 if others <= 0 else 0.0])
    return np.array(rows, dtype=float).reshape(-1, len(FEATURES))


@dataclass(frozen=True)
class Calibration:
    """score = sigmoid((raw - center) / scale); raw is the IsolationForest anomaly score."""

    center: float
    scale: float

    def score(self, raw: np.ndarray) -> np.ndarray:
        return 1.0 / (
            1.0 + np.exp(-(np.asarray(raw, dtype=float) - self.center) / self.scale)
        )


def raw_scores(forest: IsolationForest, x: np.ndarray) -> np.ndarray:
    """Higher = more unusual (sklearn's score_samples is higher for normal points)."""
    return -forest.score_samples(x)


def classification_metrics(
    predicted: np.ndarray, actual: np.ndarray
) -> dict[str, float]:
    tp = int(np.sum(predicted & actual))
    fp = int(np.sum(predicted & ~actual))
    fn = int(np.sum(~predicted & actual))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def best_cutoff(raw: np.ndarray, actual: np.ndarray) -> tuple[float, dict[str, float]]:
    """Raw cut-off with the best F1 for "raw > cut-off = outlier" (ties: fewer alarms).
    Candidates lie halfway between neighbouring raw scores, so no line sits on the cut-off."""
    values = np.unique(raw)  # ascending
    candidates = np.concatenate([[values[0] - 1e-9], (values[:-1] + values[1:]) / 2])
    best, best_metrics = None, None
    for cutoff in candidates[::-1]:  # highest cut-off (fewest alarms) first
        metrics = classification_metrics(raw > cutoff, actual)
        if best_metrics is None or metrics["f1"] > best_metrics["f1"]:
            best, best_metrics = float(cutoff), metrics
    return best, best_metrics


def calibrate(
    raw: np.ndarray, cutoff: float, threshold: float = THRESHOLD
) -> Calibration:
    """Calibration that maps the raw `cutoff` to exactly `threshold`, so
    score > threshold  <=>  raw > cutoff."""
    scale = float(np.std(raw)) or 1.0
    center = cutoff - scale * math.log(threshold / (1 - threshold))
    return Calibration(center=center, scale=scale)


class QuantityAnomalyModel(mlflow.pyfunc.PythonModel):
    """IsolationForest + per customer-material statistics + calibration, as one model.
    predict() takes order lines and returns scores from 0 (normal) to 1 (very unusual)."""

    def __init__(self, forest: IsolationForest, stats: Stats, calibration: Calibration):
        self.forest = forest
        self.stats = stats
        self.calibration = calibration

    def score(self, lines: pd.DataFrame) -> np.ndarray:
        return self.calibration.score(
            raw_scores(self.forest, features(lines, self.stats))
        )

    def predict(self, context, model_input: pd.DataFrame, params=None) -> np.ndarray:
        return self.score(model_input)


def train(
    history: pd.DataFrame, is_outlier: np.ndarray
) -> tuple[QuantityAnomalyModel, dict]:
    """Fit on the order history; calibrate the 0.8 threshold on the labelled outliers.

    `history` has LINE_COLUMNS; `is_outlier` marks the labelled outlier lines (bool array).
    Returns the model and metrics (precision, recall, f1 at the threshold, raw cut-off).
    """
    stats = history_stats(history)
    x = features(history, stats, in_history=True)
    forest = IsolationForest(n_estimators=N_ESTIMATORS, random_state=RANDOM_STATE).fit(
        x
    )
    raw = raw_scores(forest, x)
    actual = np.asarray(is_outlier, dtype=bool)
    cutoff, _ = best_cutoff(raw, actual)
    calibration = calibrate(raw, cutoff)
    model = QuantityAnomalyModel(forest, stats, calibration)
    # Check through the same path the agent uses: scores against the 0.8 threshold.
    scores = calibration.score(raw)
    metrics = classification_metrics(scores > THRESHOLD, actual)
    return model, {
        **metrics,
        "raw_cutoff": cutoff,
        "lines": len(history),
        "outliers": int(actual.sum()),
    }
