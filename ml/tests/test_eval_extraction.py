import json
import shutil
from decimal import Decimal
from pathlib import Path

import eval_extraction
import pytest
from eval_extraction import (
    evaluate,
    format_row,
    main,
    make_settings,
    results_path,
    summarize,
)
from o2c_common.schema import PurchaseOrder

POC_DATA = Path(__file__).resolve().parents[2] / "poc" / "data"
PO_IDS = ["PO-0001", "PO-0002", "PO-0003"]


@pytest.fixture
def dataset(tmp_path) -> Path:
    """Three POs from poc/data in the dataset layout (pos/, ground_truth/)."""
    root = tmp_path / "mini"
    for folder, suffix in [("pos", "pdf"), ("ground_truth", "json")]:
        (root / folder).mkdir(parents=True)
        for po_id in PO_IDS:
            shutil.copy(POC_DATA / folder / f"{po_id}.{suffix}", root / folder)
    return root


def truth(dataset: Path, po_id: str) -> PurchaseOrder:
    return PurchaseOrder.model_validate_json(
        (dataset / "ground_truth" / f"{po_id}.json").read_text(encoding="utf-8")
    )


class FakeExtract:
    """Recorded behaviour: PO-0001 perfect, PO-0002 two wrong fields, PO-0003 fails."""

    def __init__(self, dataset: Path):
        self.dataset = dataset
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, session_id: str) -> PurchaseOrder:
        self.calls.append((text, session_id))
        po_id = session_id.removeprefix(f"eval-{self.dataset.name}-")
        po = truth(self.dataset, po_id)
        if po_id == "PO-0002":
            lines = [
                po.lines[0].model_copy(update={"unit_price": Decimal(0)}),
                *po.lines[1:],
            ]
            return po.model_copy(update={"po_number": "WRONG", "lines": lines})
        if po_id == "PO-0003":
            raise RuntimeError("LLM unavailable")
        return po


def test_evaluate_scores_every_document(dataset):
    extract = FakeExtract(dataset)

    rows = evaluate(dataset, extract)

    assert [row["document"] for row in rows] == PO_IDS
    first, second, third = rows
    assert first["errors"] == [] and first["correct"] == first["total"]
    assert first["result"] == truth(dataset, "PO-0001").model_dump(mode="json")
    assert second["errors"] == ["po_number", "line.unit_price"]
    assert second["correct"] == second["total"] - 2
    assert third["errors"] == ["invalid output"] and third["correct"] == 0
    assert third["exception"] == "RuntimeError: LLM unavailable"
    assert third["result"] is None
    # The extractor gets the layout-mode PDF text and a session id per document.
    text, session_id = extract.calls[0]
    assert truth(dataset, "PO-0001").customer_name in text
    assert session_id == "eval-mini-PO-0001"


def test_evaluate_reports_progress(dataset):
    seen = []

    evaluate(dataset, FakeExtract(dataset), progress=seen.append)

    assert [row["document"] for row in seen] == PO_IDS


def test_summarize(dataset):
    rows = evaluate(dataset, FakeExtract(dataset))

    summary = summarize(rows)

    total = sum(row["total"] for row in rows)
    correct = sum(row["correct"] for row in rows)
    assert summary["documents"] == 3
    assert summary["perfect_pos"] == 1
    assert summary["field_accuracy"] == round(correct / total, 4)
    assert {e["error"]: e["count"] for e in summary["top_errors"]} == {
        "po_number": 1,
        "line.unit_price": 1,
        "invalid output": 1,
    }


def test_summarize_keeps_the_five_most_common_errors():
    rows = [{"correct": 0, "total": 1, "errors": [f"e{n}"] * n} for n in range(1, 8)]
    assert [e["error"] for e in summarize(rows)["top_errors"]] == [
        "e7",
        "e6",
        "e5",
        "e4",
        "e3",
    ]
    assert summarize([])["field_accuracy"] == 0.0


def test_format_row():
    assert format_row(
        {"document": "PO-0001", "correct": 20, "total": 20, "errors": []}
    ) == ("PO-0001   20/20   perfect")
    assert "po_number, line.unit_price" in format_row(
        {
            "document": "PO-0002",
            "correct": 18,
            "total": 20,
            "errors": ["po_number", "line.unit_price"],
        }
    )


def test_results_path_is_safe_for_model_names():
    path = results_path(Path("ml/data/holdout"), "openai/gpt-x:latest")
    assert path == eval_extraction.RESULTS_DIR / "holdout-openai_gpt-x_latest.json"


def test_make_settings_needs_only_litellm(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")

    assert make_settings().litellm_master_key == "test-key"


def test_main_writes_results(dataset, tmp_path, monkeypatch, capsys):
    calls = []

    def fake_make_extractor(settings, model):
        calls.append(model)
        return FakeExtract(dataset)

    monkeypatch.setattr(eval_extraction, "make_extractor", fake_make_extractor)
    monkeypatch.setattr(eval_extraction, "make_settings", lambda: None)
    monkeypatch.setattr(eval_extraction, "RESULTS_DIR", tmp_path / "results")

    out = main([str(dataset), "o2c-extract-candidate"])

    assert calls == ["o2c-extract-candidate"]
    assert out == tmp_path / "results" / "mini-o2c-extract-candidate.json"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["model"] == "o2c-extract-candidate"
    assert saved["summary"]["documents"] == 3
    assert [d["document"] for d in saved["documents"]] == PO_IDS
    printed = capsys.readouterr().out
    assert "PO-0001" in printed and '"perfect_pos": 1' in printed


def test_main_defaults_to_o2c_extract(dataset, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        eval_extraction,
        "make_extractor",
        lambda s, model: calls.append(model) or FakeExtract(dataset),
    )
    monkeypatch.setattr(eval_extraction, "make_settings", lambda: None)
    monkeypatch.setattr(eval_extraction, "RESULTS_DIR", tmp_path)

    assert main([str(dataset)]).name == "mini-o2c-extract.json"
    assert calls == ["o2c-extract"]


def test_main_without_arguments_shows_usage():
    with pytest.raises(SystemExit, match="Usage"):
        main([])
