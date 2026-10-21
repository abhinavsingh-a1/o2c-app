"""Training helpers without training: data loading and the stratified split."""

from collections import Counter

import pytest

pytest.importorskip("torch")  # the "ml" dependency group
import train_classifier
from classifier_common import LABEL2ID, MAX_CHARS
from train_classifier import PARAMS, load_documents, stratified_split


def test_hyperparameters_match_the_plan():
    assert PARAMS["base_model"] == "distilbert-base-multilingual-cased"
    assert (PARAMS["max_length"], PARAMS["batch_size"], PARAMS["epochs"]) == (
        256,
        16,
        3,
    )
    assert PARAMS["learning_rate"] == 5e-5
    assert PARAMS["device"] == "cpu"


def test_load_documents_reads_labels_and_prepares_text():
    files, texts, labels = load_documents(train_classifier.HOLDOUT_DIR)

    assert len(files) == len(texts) == len(labels) == 40
    assert Counter(labels) == {i: 10 for i in LABEL2ID.values()}
    assert all(0 < len(text) <= MAX_CHARS for text in texts)


def test_stratified_split_is_80_20_per_class_and_fixed():
    labels = [i % 4 for i in range(240)]  # 60 per class, like classifier/train

    train, validation = stratified_split(labels, seed=2026)

    assert len(train) == 192 and len(validation) == 48
    assert not set(train) & set(validation)
    assert Counter(labels[i] for i in validation) == {0: 12, 1: 12, 2: 12, 3: 12}
    assert stratified_split(labels, seed=2026) == (train, validation)
    assert stratified_split(labels, seed=1) != (train, validation)


def test_model_requirements_pin_the_runtime_without_torchvision():
    requirements = train_classifier.model_requirements()

    names = [r.split("==")[0] for r in requirements]
    assert names == ["torch", "transformers", "tokenizers", "mlflow-skinny"]
    assert all("+" not in r for r in requirements)
    assert "mlflow-skinny==2.17.2" in requirements
