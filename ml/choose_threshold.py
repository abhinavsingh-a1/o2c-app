"""Choose the hybrid classifier's non-PO threshold on the VALIDATION split (ADR-009).

Usage: uv run --group ml python ml/choose_threshold.py

Rebuilds the exact train/validation split of ml/train_classifier.py (same datasets, same
seed) and classifies the validation documents with models:/o2c-doc-classifier@champion
(MLFLOW_TRACKING_URI, default http://localhost:5000). Risk-based routing accepts
DistilBERT only for non-PO predictions at or above CLASSIFIER_NON_PO_THRESHOLD; purchase
order predictions always go to GPT. The threshold is the lowest value at which every
non-PO prediction on validation is correct, plus MARGIN. Prints the coverage at that
value (share of documents DistilBERT decides alone). The holdout is not used. Writes
ml/results/classifier-threshold-validation.json.
"""

import json
import math
import os

from classifier.hybrid import NON_PO_CONFIDENT, PURCHASE_ORDER, load_distilbert, route
from classifier_common import ID2LABEL
from train_classifier import (
    PARAMS,
    RESULTS_DIR,
    TRAIN_DIRS,
    load_training,
    stratified_split,
)

MARGIN = 0.005


def lowest_safe_threshold(predictions: list[tuple[bool, float]]) -> float:
    """Lowest t such that every prediction with confidence >= t is correct:
    just above the most confident mistake, 0.0 without mistakes."""
    wrong = [confidence for correct, confidence in predictions if not correct]
    return math.nextafter(max(wrong), math.inf) if wrong else 0.0


def choose_threshold(
    predictions: list[tuple[bool, float]], margin: float = MARGIN
) -> float:
    """lowest_safe_threshold plus the margin, rounded up to 3 decimals, at most 1.0."""
    raw = lowest_safe_threshold(predictions) + margin
    return min(1.0, math.ceil(round(raw * 1000, 6)) / 1000)


def non_po_predictions(rows: list[dict]) -> list[tuple[bool, float]]:
    """(correct, confidence) of the predictions that are not purchase_order."""
    return [
        (r["predicted"] == r["expected"], r["confidence"])
        for r in rows
        if r["predicted"] != PURCHASE_ORDER
    ]


def coverage(rows: list[dict], threshold: float) -> float:
    """Share of all documents that DistilBERT decides alone (routing non_po_confident)."""
    accepted = sum(
        route(r["predicted"], r["confidence"], threshold) == NON_PO_CONFIDENT
        for r in rows
    )
    return accepted / len(rows)


def main() -> float:
    files, texts, labels = load_training(TRAIN_DIRS)
    _, val_idx = stratified_split(labels, PARAMS["seed"])
    model = load_distilbert(
        os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    )
    if model is None:
        raise SystemExit("model unavailable")

    rows = []
    for i in val_idx:
        predicted, confidence = model.predict(texts[i])
        rows.append(
            {
                "file": files[i],
                "expected": ID2LABEL[labels[i]],
                "predicted": predicted,
                "confidence": round(confidence, 6),
            }
        )
    non_po = non_po_predictions(rows)
    safe = lowest_safe_threshold(non_po)
    threshold = choose_threshold(non_po)
    mistakes = [r for r in rows if r["predicted"] != r["expected"]]

    print(f"validation: {len(rows)} documents, {len(mistakes)} mistakes")
    for m in mistakes:
        print(
            f"  {m['file']}: {m['expected']} -> {m['predicted']} p={m['confidence']:.4f}"
        )
    wrong_non_po = sum(not correct for correct, _ in non_po)
    lowest = min(c for _, c in non_po)
    print(
        f"non-PO predictions: {len(non_po)}, {wrong_non_po} wrong, "
        f"lowest confidence {lowest:.4f}"
    )
    print(f"lowest threshold with all non-PO predictions correct: {safe:.4f}")
    print(f"non-PO threshold (+{MARGIN} margin): {threshold:.3f}")
    share = coverage(rows, threshold)
    print(
        f"coverage: DistilBERT decides {share:.1%} of validation documents alone, "
        f"{1 - share:.1%} go to GPT"
    )

    out = RESULTS_DIR / "classifier-threshold-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "routing": "risk-based (non-PO only)",
                "lowest_safe_threshold": safe,
                "margin": MARGIN,
                "non_po_threshold": threshold,
                "coverage": share,
                "documents": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return threshold


if __name__ == "__main__":
    main()
