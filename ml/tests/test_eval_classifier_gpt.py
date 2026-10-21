import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import eval_classifier_gpt
import pytest
from classifier.app import Classification
from eval_classifier_gpt import (
    DOC_TYPES,
    UsageRecorder,
    evaluate,
    main,
    make_classify_text,
    read_labels,
    summarize,
)

POC_DATA = Path(__file__).resolve().parents[2] / "poc" / "data"
USAGE = {"prompt_tokens": 900, "completion_tokens": 40, "cost_usd": 0.000424}


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

    def __call__(self, text: str, file: str) -> tuple[Classification, dict]:
        self.calls.append((text, file))
        expected = self.labels[file]
        if expected == "other":
            raise TimeoutError("LiteLLM timeout")
        doc_type = "purchase_order" if expected == "remittance" else expected
        return Classification(doc_type=doc_type, reason=f"looks like {doc_type}"), USAGE


def test_doc_types_match_the_classifier_service():
    literal = Classification.model_fields["doc_type"].annotation
    assert DOC_TYPES == list(literal.__args__)


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
    assert rows[0]["usage"] == USAGE and rows[3]["usage"] == {}
    assert all(r["ms"] >= 0 for r in rows)
    text, file = classify_text.calls[0]
    assert file == rows[0]["file"]
    assert "PURCHASE ORDER" in text.upper() or "BESTELLUNG" in text.upper()


def test_summary_has_accuracy_time_and_cost(dataset):
    summary = summarize(evaluate(dataset, FakeClassifyText(dataset)))

    assert summary["accuracy"] == 0.5
    assert summary["confusion_matrix"]["other"]["error"] == 1
    assert summary["ms_per_document"] >= 0
    assert summary["usage"] == {
        "prompt_tokens": 2700,
        "completion_tokens": 120,
        "cost_usd": round(3 * 0.000424, 6),
        "cost_per_1000_documents_usd": 0.424,
    }


def fake_raw_response(cost: str | None):
    usage = SimpleNamespace(prompt_tokens=900, completion_tokens=40)
    response = SimpleNamespace(usage=usage, choices=[])
    headers = {"x-litellm-response-cost": cost} if cost is not None else {}
    return SimpleNamespace(parse=lambda: response, headers=headers), response


def test_usage_recorder_keeps_tokens_and_litellm_cost():
    raw, response = fake_raw_response("0.000424")
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return raw

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
    )
    recorder = UsageRecorder(client)

    assert (
        recorder.chat.completions.create(model="o2c-extract", messages=[]) is response
    )
    assert sent == [{"model": "o2c-extract", "messages": []}]
    assert recorder.last == USAGE


def test_usage_recorder_without_cost_header():
    raw, _ = fake_raw_response(None)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=lambda **_: raw)
            )
        )
    )
    recorder = UsageRecorder(client)

    recorder.chat.completions.create(model="o2c-extract", messages=[])

    assert recorder.last["cost_usd"] is None
    assert recorder.last["prompt_tokens"] == 900


def test_make_classify_text_calls_classify_through_litellm(monkeypatch):
    created, calls = [], []

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            created.append((base_url, api_key))

    def fake_classify(llm, system_prompt, text, event):
        calls.append((llm, system_prompt, text, event))
        llm.last = USAGE  # as if UsageRecorder had seen the call
        return Classification(doc_type="invoice", reason="bill")

    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000")
    monkeypatch.setattr(eval_classifier_gpt, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(eval_classifier_gpt, "classify", fake_classify)

    result, usage = make_classify_text()("invoice text", "other/DOC-0001.pdf")

    assert result.doc_type == "invoice"
    assert usage == USAGE
    assert created == [("http://litellm:4000", "test-key")]
    llm, system_prompt, text, event = calls[0]
    assert isinstance(llm, UsageRecorder)
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
    assert saved["classifier"] == "gpt"
    assert saved["accuracy"] == 0.5
    assert len(saved["documents"]) == 4
    assert len(saved["mistakes"]) == 2
    assert saved["usage"]["cost_per_1000_documents_usd"] == 0.424
    printed = capsys.readouterr().out
    assert "accuracy: 50.0% (4 documents)" in printed
    assert "expected \\ predicted" in printed
    assert "mistakes (2):" in printed


def test_main_without_arguments_shows_usage():
    with pytest.raises(SystemExit, match="Usage"):
        main([])
