import math

import pytest
from choose_threshold import (
    MARGIN,
    choose_threshold,
    coverage,
    lowest_safe_threshold,
    non_po_predictions,
)
from classifier.hybrid import MAX_CHARS, MAX_LENGTH
from classifier_common import MAX_CHARS as TRAINING_MAX_CHARS
from train_classifier import PARAMS

VALIDATION = [(True, 0.99), (True, 0.97), (False, 0.96), (True, 0.72), (False, 0.983)]


def test_lowest_safe_threshold_is_just_above_the_most_confident_mistake():
    threshold = lowest_safe_threshold(VALIDATION)

    assert threshold > 0.983
    assert threshold == math.nextafter(0.983, math.inf)
    # every prediction at or above it is correct
    assert all(correct for correct, c in VALIDATION if c >= threshold)


def test_without_mistakes_every_threshold_is_safe():
    assert lowest_safe_threshold([(True, 0.5), (True, 0.9)]) == 0.0


def test_choose_threshold_adds_the_margin_rounded_up():
    assert choose_threshold(VALIDATION) == 0.988  # 0.983 + 0.005
    assert choose_threshold(VALIDATION, margin=0.0101) == 0.994
    assert choose_threshold([(True, 0.9)], margin=0.05) == 0.05


def test_choose_threshold_is_at_most_one():
    assert choose_threshold(VALIDATION, margin=0.02) == 1.0


def test_margin_is_small():
    assert 0 < MARGIN <= 0.01


ROWS = [
    {"expected": "purchase_order", "predicted": "purchase_order", "confidence": 0.99},
    {"expected": "other", "predicted": "purchase_order", "confidence": 0.98},
    {"expected": "invoice", "predicted": "invoice", "confidence": 0.97},
    {"expected": "other", "predicted": "remittance", "confidence": 0.80},
    {"expected": "other", "predicted": "other", "confidence": 0.60},
]


def test_non_po_predictions_ignore_purchase_order_predictions():
    # The confident false PO (other -> purchase_order) does not count: GPT checks every PO.
    assert non_po_predictions(ROWS) == [(True, 0.97), (False, 0.80), (True, 0.60)]


def test_non_po_threshold_is_above_the_most_confident_non_po_mistake():
    assert choose_threshold(non_po_predictions(ROWS)) == 0.805  # 0.80 + 0.005


def test_without_non_po_mistakes_only_the_margin_remains():
    assert choose_threshold(non_po_predictions(ROWS[:3])) == MARGIN


def test_coverage_counts_only_confident_non_po_predictions():
    assert coverage(ROWS, 0.805) == pytest.approx(1 / 5)  # the invoice
    assert coverage(ROWS, 0.0) == pytest.approx(3 / 5)  # every non-PO prediction
    assert coverage(ROWS, 1.0) == 0.0


def test_service_input_matches_training():
    """The service must feed DistilBERT exactly what it was trained on."""
    assert MAX_CHARS == TRAINING_MAX_CHARS
    assert MAX_LENGTH == PARAMS["max_length"]
