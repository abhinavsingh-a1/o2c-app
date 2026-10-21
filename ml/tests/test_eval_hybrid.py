import json
import shutil
from pathlib import Path

import eval_hybrid
import pytest
from classifier.hybrid import HybridResult
from eval_hybrid import evaluate, main, summarize_hybrid

CLASSIFIER_HOLDOUT = (
    Path(__file__).resolve().parents[1] / "data" / "classifier" / "holdout"
)


@pytest.fixture
def dataset(tmp_path) -> Path:
    """Two documents of the classifier holdout with their labels.csv rows."""
    rows = (CLASSIFIER_HOLDOUT / "labels.csv").read_text(encoding="utf-8").splitlines()
    header, picked = rows[0], [rows[1], rows[-1]]
    root = tmp_path / "mini"
    for row in picked:
        file = row.split(",")[0]
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(CLASSIFIER_HOLDOUT / file, root / file)
    (root / "labels.csv").write_text(
        "\n".join([header, *picked]) + "\n", encoding="utf-8"
    )
    return root


def labels(dataset: Path) -> list[str]:
    return [
        line.split(",")[1]
        for line in (dataset / "labels.csv").read_text().splitlines()[1:]
    ]


def recorded(dataset: Path):
    """First document: DistilBERT, right. Second: GPT, a false purchase order, with a cost."""
    first, _ = labels(dataset)

    def classify(text: str, file: str):
        if not hasattr(classify, "n"):
            classify.n = 0
        classify.n += 1
        if classify.n == 1:
            return HybridResult(
                first, "DistilBERT, p=0.995", "distilbert", 0.995, "non_po_confident"
            ), {}
        wrong = "purchase_order"  # a false purchase order
        usage = {"prompt_tokens": 900, "completion_tokens": 30, "cost_usd": 0.0004}
        return HybridResult(wrong, "GPT", "gpt", 0.9, "po_double_check"), usage

    return classify


def test_evaluate_records_who_classified(dataset):
    rows = evaluate(dataset, recorded(dataset))

    assert [r["classified_by"] for r in rows] == ["distilbert", "gpt"]
    assert [r["confidence"] for r in rows] == [0.995, 0.9]
    assert [r["routing_reason"] for r in rows] == [
        "non_po_confident",
        "po_double_check",
    ]
    assert rows[0]["predicted"] == rows[0]["expected"]
    assert rows[1]["predicted"] != rows[1]["expected"]
    assert all(r["ms"] >= 0 for r in rows)


def test_evaluate_counts_a_failure_as_error(dataset):
    def broken(text, file):
        raise RuntimeError("LiteLLM down")

    rows = evaluate(dataset, broken)

    assert {r["predicted"] for r in rows} == {"error"}
    assert {r["routing_reason"] for r in rows} == {"model_unavailable"}
    assert rows[0]["reason"] == "RuntimeError: LiteLLM down"


def test_summarize_hybrid(dataset):
    summary = summarize_hybrid(evaluate(dataset, recorded(dataset)))

    assert summary["accuracy"] == 0.5
    assert summary["sent_to_gpt"] == 1
    assert summary["gpt_share"] == 0.5
    assert summary["false_purchase_orders"] == 1
    assert summary["routing"] == {"non_po_confident": 1, "po_double_check": 1}
    # cost over ALL documents: 0.0004 for two documents -> $0.20 per 1000
    assert summary["cost_per_1000_documents_usd"] == 0.2


def test_main_writes_results(dataset, tmp_path, monkeypatch, capsys):
    thresholds = []
    monkeypatch.setattr(
        eval_hybrid,
        "make_classify_hybrid",
        lambda non_po_threshold: (
            thresholds.append(non_po_threshold) or recorded(dataset)
        ),
    )
    monkeypatch.setattr(eval_hybrid, "RESULTS_DIR", tmp_path / "results")

    out = main([str(dataset), "0.95"])

    assert thresholds == [0.95]
    assert out.name == "classifier-hybrid-mini.json"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["non_po_threshold"] == 0.95 and saved["gpt_share"] == 0.5
    assert saved["false_purchase_orders"] == 1
    assert "1/2 (50.0%) sent to GPT" in capsys.readouterr().out


def test_main_defaults_to_service_threshold(dataset, tmp_path, monkeypatch):
    thresholds = []
    monkeypatch.setattr(
        eval_hybrid,
        "make_classify_hybrid",
        lambda non_po_threshold: (
            thresholds.append(non_po_threshold) or recorded(dataset)
        ),
    )
    monkeypatch.setattr(eval_hybrid, "RESULTS_DIR", tmp_path)

    main([str(dataset)])

    assert thresholds == [eval_hybrid.DEFAULT_NON_PO_THRESHOLD]
