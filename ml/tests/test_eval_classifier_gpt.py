import json
import shutil
from pathlib import Path

import eval_classifier_gpt
import pytest
from classifier.app import Classification
from eval_classifier_gpt import (
    DOC_TYPES,
    confusion_matrix,
    evaluate,
    format_matrix,
    main,
    make_classify_text,
    read_labels,
    summarize,
)

POC_DATA = Path(__file__).resolve().parents[2] / "poc" / "data"


@pytest.fixture
def dataset(tmp_path) -> Path:
    """One document per type from poc/data, with labels.csv like ml/data/classifier."""
    doc_types = json.loads((POC_DATA / "doc_types.json").read_text(encoding="utf-8"))
    root = tmp_path / "mini"
    rows = ["file,doc_type"]
    for doc_type in DOC_TYPES:
        name = next(n for n, t in doc_types.items() if t == doc_type)
        folder = "pos" if doc_type == "purchase_order" else "other"
        (root / folder).mkdir(parents=True, exist_ok=True)
        shutil.copy(POC_DATA / folder / name, root / folder / name)
        rows.append(f"{folder}/{name},{doc_type}")
    (root / "labels.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root


class FakeClassifyText:
    """Recorded answers: right for the PO and the invoice, the remittance is called
    a purchase order, and the call for the other document fails."""

    def __init__(self, dataset: Path):
        self.labels = dict(read_labels(dataset))
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, file: str) -> Classification:
        self.calls.append((text, file))
        expected = self.labels[file]
        if expected == "other":
            raise TimeoutError("LiteLLM timeout")
        doc_type = "purchase_order" if expected == "remittance" else expected
        return Classification(doc_type=doc_type, reason=f"looks like {doc_type}")


def test_read_labels(dataset):
    labels = read_labels(dataset)
    assert [doc_type for _, doc_type in labels] == DOC_TYPES
    assert labels[0][0].startswith("pos/PO-")


def test_evaluate_classifies_every_document(dataset):
    classify_text = FakeClassifyText(dataset)

    rows = evaluate(dataset, classify_text)

    assert [(r["expected"], r["predicted"]) for r in rows] == [
        ("purchase_order", "purchase_order"),
        ("invoice", "invoice"),
        ("remittance", "purchase_order"),
        ("other", "error"),
    ]
    assert rows[0]["reason"] == "looks like purchase_order"
    assert rows[3]["reason"] == "TimeoutError: LiteLLM timeout"
    text, file = classify_text.calls[0]
    assert file == rows[0]["file"]
    assert "PURCHASE ORDER" in text.upper() or "BESTELLUNG" in text.upper()


def test_confusion_matrix_and_summary(dataset):
    rows = evaluate(dataset, FakeClassifyText(dataset))

    summary = summarize(rows)

    assert summary["documents"] == 4
    assert summary["accuracy"] == 0.5
    matrix = summary["confusion_matrix"]
    assert list(matrix) == DOC_TYPES
    assert list(matrix["other"]) == [*DOC_TYPES, "error"]
    assert matrix["remittance"]["purchase_order"] == 1
    assert matrix["other"]["error"] == 1
    assert matrix["invoice"]["invoice"] == 1
    assert [m["expected"] for m in summary["mistakes"]] == ["remittance", "other"]


def test_confusion_matrix_has_no_error_column_without_failures():
    rows = [{"expected": t, "predicted": t} for t in DOC_TYPES]
    matrix = confusion_matrix(rows)
    assert all(list(counts) == DOC_TYPES for counts in matrix.values())
    assert summarize(rows)["accuracy"] == 1.0
    assert summarize([])["accuracy"] == 0.0


def test_format_matrix():
    rows = [{"expected": "invoice", "predicted": "invoice"}]

    lines = format_matrix(confusion_matrix(rows)).splitlines()

    assert lines[0].split() == ["expected", "\\", "predicted", *DOC_TYPES]
    assert lines[2].split() == ["invoice", "0", "1", "0", "0"]
    assert len(lines) == 1 + len(DOC_TYPES)


def test_make_classify_text_calls_classify_through_litellm(monkeypatch):
    created, calls = [], []

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            created.append((base_url, api_key))

    def fake_classify(llm, system_prompt, text, event):
        calls.append((llm, system_prompt, text, event))
        return Classification(doc_type="invoice", reason="bill")

    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000")
    monkeypatch.setattr(eval_classifier_gpt, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(eval_classifier_gpt, "classify", fake_classify)

    result = make_classify_text()("invoice text", "other/DOC-0001.pdf")

    assert result.doc_type == "invoice"
    assert created == [("http://litellm:4000", "test-key")]
    _, system_prompt, text, event = calls[0]
    assert "{schema}" not in system_prompt and "purchase_order" in system_prompt
    assert text == "invoice text"
    assert event.payload == {"file": "other/DOC-0001.pdf"}
    # Same file -> same session id, so reruns group together in Langfuse.
    make_classify_text()("invoice text", "other/DOC-0001.pdf")
    assert calls[1][3].correlation_id == event.correlation_id


def test_main_prints_and_writes_results(dataset, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        eval_classifier_gpt, "make_classify_text", lambda: FakeClassifyText(dataset)
    )
    monkeypatch.setattr(eval_classifier_gpt, "RESULTS_DIR", tmp_path / "results")

    out = main([str(dataset)])

    assert out == tmp_path / "results" / "classifier-gpt-mini.json"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["accuracy"] == 0.5
    assert len(saved["documents"]) == 4
    assert len(saved["mistakes"]) == 2
    printed = capsys.readouterr().out
    assert "accuracy: 50.0% (4 documents)" in printed
    assert "expected \\ predicted" in printed
    assert "mistakes (2):" in printed


def test_main_without_arguments_shows_usage():
    with pytest.raises(SystemExit, match="Usage"):
        main([])
