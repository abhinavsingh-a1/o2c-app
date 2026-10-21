"""Fast tests for label mapping, text preparation and the shared summary (no torch)."""

import json
from pathlib import Path

import pytest
from classifier_common import (
    ID2LABEL,
    LABEL2ID,
    LABELS,
    MAX_CHARS,
    confusion_matrix,
    format_matrix,
    label_ids,
    label_names,
    prepare_text,
    read_labels,
    summarize,
    write_results,
)
from o2c_common.pdf import pdf_text

ROOT = Path(__file__).resolve().parents[2]
POC_DATA = ROOT / "poc" / "data"


def test_label_mapping_round_trip():
    assert LABELS == ["purchase_order", "invoice", "remittance", "other"]
    assert LABEL2ID == {"purchase_order": 0, "invoice": 1, "remittance": 2, "other": 3}
    assert ID2LABEL == {0: "purchase_order", 1: "invoice", 2: "remittance", 3: "other"}
    assert label_ids(["other", "purchase_order", "remittance"]) == [3, 0, 2]
    assert label_names([3, 0, 2]) == ["other", "purchase_order", "remittance"]
    assert label_names(label_ids(LABELS)) == LABELS


def test_unknown_label_is_rejected():
    with pytest.raises(KeyError):
        label_ids(["order_confirmation"])


def test_every_dataset_label_is_known():
    for dataset in ["classifier/train", "classifier/holdout"]:
        labels = [t for _, t in read_labels(ROOT / "ml" / "data" / dataset)]
        assert set(labels) == set(LABELS)
        label_ids(labels)


def test_prepare_text_is_the_start_of_the_layout_text():
    pdf = (POC_DATA / "pos" / "PO-0001.pdf").read_bytes()

    text = prepare_text(pdf)

    assert MAX_CHARS == 1500
    assert len(text) == MAX_CHARS
    assert text == pdf_text(pdf)[:MAX_CHARS]
    po = json.loads(
        (POC_DATA / "ground_truth" / "PO-0001.json").read_text(encoding="utf-8")
    )
    assert po["po_number"] in text  # the header is at the start


def test_prepare_text_keeps_short_documents_whole(tmp_path):
    doc_types = json.loads((POC_DATA / "doc_types.json").read_text(encoding="utf-8"))
    for name, doc_type in doc_types.items():
        folder = "pos" if doc_type == "purchase_order" else "other"
        pdf = (POC_DATA / folder / name).read_bytes()
        full = pdf_text(pdf)
        if len(full) < MAX_CHARS:
            assert prepare_text(pdf) == full
            return
    pytest.skip("no short document in poc/data")


def test_summarize_and_confusion_matrix():
    rows = [
        {
            "file": "a",
            "expected": "invoice",
            "predicted": "invoice",
            "reason": None,
            "ms": 10,
        },
        {
            "file": "b",
            "expected": "remittance",
            "predicted": "invoice",
            "reason": None,
            "ms": 30,
        },
        {
            "file": "c",
            "expected": "other",
            "predicted": "error",
            "reason": "x",
            "ms": 20,
        },
    ]

    summary = summarize(rows)

    assert summary["documents"] == 3
    assert summary["accuracy"] == round(1 / 3, 4)
    assert summary["ms_per_document"] == 20.0
    assert "usage" not in summary  # local model: no tokens, no cost
    assert [m["file"] for m in summary["mistakes"]] == ["b", "c"]
    matrix = summary["confusion_matrix"]
    assert list(matrix) == LABELS
    assert list(matrix["other"]) == [*LABELS, "error"]
    assert matrix["remittance"]["invoice"] == 1 and matrix["other"]["error"] == 1


def test_confusion_matrix_has_no_error_column_without_failures():
    rows = [{"expected": t, "predicted": t} for t in LABELS]
    assert all(list(counts) == LABELS for counts in confusion_matrix(rows).values())
    assert summarize(rows)["accuracy"] == 1.0
    assert summarize([])["accuracy"] == 0.0


def test_format_matrix():
    lines = format_matrix(
        confusion_matrix([{"expected": "invoice", "predicted": "invoice"}])
    ).splitlines()

    assert lines[0].split() == ["expected", "\\", "predicted", *LABELS]
    assert lines[2].split() == ["invoice", "0", "1", "0", "0"]
    assert len(lines) == 1 + len(LABELS)


def test_write_results(tmp_path):
    out = write_results(
        tmp_path / "r" / "x.json",
        Path("ml/data/classifier/holdout"),
        {"accuracy": 1.0},
        [],
        classifier="distilbert",
    )

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved == {
        "dataset": str(Path("ml/data/classifier/holdout")),
        "classifier": "distilbert",
        "accuracy": 1.0,
        "documents": [],
    }
