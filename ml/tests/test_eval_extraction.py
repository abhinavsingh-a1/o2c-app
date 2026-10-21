import json
import shutil
from decimal import Decimal
from pathlib import Path

import eval_extraction
import httpx
import pytest
from eval_extraction import (
    ResponseRecorder,
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

    def fake_make_extractor(settings, model, http_client=None):
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
        lambda s, model, **kw: calls.append(model) or FakeExtract(dataset),
    )
    monkeypatch.setattr(eval_extraction, "make_settings", lambda: None)
    monkeypatch.setattr(eval_extraction, "RESULTS_DIR", tmp_path)

    assert main([str(dataset)]).name == "mini-o2c-extract.json"
    assert calls == ["o2c-extract"]


def test_main_without_arguments_shows_usage(capsys):
    with pytest.raises(SystemExit):
        main([])
    assert "eval_extraction.py <dataset dir>" in capsys.readouterr().err


def test_evaluate_with_limit_takes_the_first_documents(dataset):
    rows = evaluate(dataset, FakeExtract(dataset), limit=2)

    assert [row["document"] for row in rows] == PO_IDS[:2]


def test_main_with_limit_names_the_results_file(dataset, tmp_path, monkeypatch):
    monkeypatch.setattr(
        eval_extraction, "make_extractor", lambda s, model, **kw: FakeExtract(dataset)
    )
    monkeypatch.setattr(eval_extraction, "make_settings", lambda: None)
    monkeypatch.setattr(eval_extraction, "RESULTS_DIR", tmp_path)

    out = main([str(dataset), "--limit", "1"])

    assert out.name == "mini-first1-o2c-extract.json"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["limit"] == 1
    assert saved["summary"]["documents"] == 1


def litellm_response(cost: str | None, fallbacks: str | None = None) -> httpx.Response:
    headers = {}
    if cost is not None:
        headers["x-litellm-response-cost"] = cost
    if fallbacks is not None:
        headers["x-litellm-attempted-fallbacks"] = fallbacks
    return httpx.Response(200, headers=headers)


def test_response_recorder_sums_cost_and_counts_fallbacks():
    recorder = ResponseRecorder()
    recorder(litellm_response("0.0012"))
    recorder(litellm_response("0.0003", fallbacks="1"))
    recorder(litellm_response(None, fallbacks="0"))

    assert recorder.take() == {"cost_usd": 0.0015, "fallbacks": 1}
    assert recorder.take() == {"cost_usd": None, "fallbacks": 0}  # reset


def test_response_recorder_client_calls_the_hook():
    recorder = ResponseRecorder()
    transport = httpx.MockTransport(lambda request: litellm_response("0.002"))
    client = recorder.client()
    client._transport = transport  # the hook is what matters, not the network

    client.post("http://litellm/v1/chat/completions")

    assert recorder.take() == {"cost_usd": 0.002, "fallbacks": 0}


class CostlyExtract(FakeExtract):
    """FakeExtract whose calls show up in the recorder like LiteLLM responses."""

    def __init__(self, dataset: Path, recorder: ResponseRecorder):
        super().__init__(dataset)
        self.recorder = recorder

    def __call__(self, text: str, session_id: str) -> PurchaseOrder:
        self.recorder(
            litellm_response("0.001", fallbacks="1" if "0002" in session_id else None)
        )
        return super().__call__(text, session_id)


def test_evaluate_records_latency_cost_and_fallbacks(dataset):
    recorder = ResponseRecorder()
    recorder(litellm_response("9.9"))  # before the first document: not counted

    rows = evaluate(dataset, CostlyExtract(dataset, recorder), recorder=recorder)

    assert [row["cost_usd"] for row in rows] == [0.001, 0.001, 0.001]
    assert [row["fallbacks"] for row in rows] == [0, 1, 0]
    assert all(row["latency_s"] >= 0 for row in rows)


def test_evaluate_without_recorder_has_no_cost(dataset):
    rows = evaluate(dataset, FakeExtract(dataset))

    assert {row["cost_usd"] for row in rows} == {None}
    assert {row["fallbacks"] for row in rows} == {0}


def test_summarize_latency_cost_and_fallbacks():
    rows = [
        {
            "correct": 1,
            "total": 1,
            "errors": [],
            "latency_s": 1.0,
            "cost_usd": 0.002,
            "fallbacks": 0,
        },
        {
            "correct": 1,
            "total": 1,
            "errors": [],
            "latency_s": 2.0,
            "cost_usd": 0.004,
            "fallbacks": 1,
        },
        {
            "correct": 0,
            "total": 1,
            "errors": ["x"],
            "latency_s": 3.0,
            "cost_usd": None,
            "fallbacks": 0,
        },
    ]

    summary = summarize(rows)

    assert summary["avg_latency_s"] == 2.0
    assert summary["cost_per_po_usd"] == 0.003  # documents without a cost are left out
    assert summary["fallbacks"] == 1
    assert summarize([])["avg_latency_s"] is None
    assert summarize([])["cost_per_po_usd"] is None


def test_format_row_shows_latency():
    row = {
        "document": "PO-0001",
        "correct": 20,
        "total": 20,
        "errors": [],
        "latency_s": 2.345,
    }
    assert format_row(row) == "PO-0001   20/20   2.3s  perfect"


def test_main_gives_the_extractor_a_recording_client(dataset, tmp_path, monkeypatch):
    clients = []
    monkeypatch.setattr(
        eval_extraction,
        "make_extractor",
        lambda s, model, http_client=None: (
            clients.append(http_client) or FakeExtract(dataset)
        ),
    )
    monkeypatch.setattr(eval_extraction, "make_settings", lambda: None)
    monkeypatch.setattr(eval_extraction, "RESULTS_DIR", tmp_path)

    out = main([str(dataset)])

    assert isinstance(clients[0], httpx.Client)
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
    assert {"avg_latency_s", "cost_per_po_usd", "fallbacks"} <= set(summary)
