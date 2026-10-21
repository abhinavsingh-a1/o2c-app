import json

import compare_extraction
import pytest
from compare_extraction import (
    DATASETS,
    compare_dataset,
    format_table,
    load_summaries,
    main,
    should_migrate,
    table_rows,
)

CURRENT = {
    "documents": 20,
    "perfect_pos": 9,
    "field_accuracy": 0.9901,
    "avg_latency_s": 4.0,
    "cost_per_po_usd": 0.002,
    "fallbacks": 0,
}


def summary(**changes) -> dict:
    return {**CURRENT, **changes}


def everywhere(s: dict) -> dict[str, dict]:
    return {label: s for label, _ in DATASETS}


def test_better_candidate_passes():
    assert compare_dataset(CURRENT, summary(perfect_pos=12, field_accuracy=0.995)) == []


def test_fewer_perfect_pos_fails():
    assert compare_dataset(CURRENT, summary(perfect_pos=8)) == ["perfect POs 8 < 9"]


def test_field_accuracy_may_drop_at_most_0_2_points():
    assert compare_dataset(CURRENT, summary(field_accuracy=0.9881)) == []  # -0.20
    assert compare_dataset(CURRENT, summary(field_accuracy=0.9880)) == [
        "field accuracy 0.21 points lower"
    ]


def test_cost_may_rise_at_most_50_percent():
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.003)) == []
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.0031)) == [
        "cost per PO +55%"
    ]
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.0001)) == []


def test_unmeasured_cost_fails():
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=None)) == [
        "cost per PO not measured"
    ]


def test_should_migrate_needs_every_dataset():
    candidate = everywhere(summary(perfect_pos=10))
    assert should_migrate(everywhere(CURRENT), candidate) == (True, [])

    candidate["holdout"] = summary(perfect_pos=8)
    ok, reasons = should_migrate(everywhere(CURRENT), candidate)
    assert not ok
    assert reasons == ["holdout: perfect POs 8 < 9"]


def write_results(results_dir, model: str, s: dict) -> None:
    for _, prefix in DATASETS:
        (results_dir / f"{prefix}-{model}.json").write_text(
            json.dumps({"summary": s}), encoding="utf-8"
        )


def test_load_summaries(tmp_path):
    write_results(tmp_path, "m", CURRENT)

    assert load_summaries("m", tmp_path) == everywhere(CURRENT)


def test_table_rows_and_format():
    rows = table_rows({"o2c-extract": {"holdout": summary(cost_per_po_usd=None)}})

    assert rows == [["o2c-extract", "holdout", "9/20", "99.01%", "4.0 s", "n/a", "0"]]
    lines = format_table(rows).splitlines()
    assert lines[0].startswith("model")
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert "99.01%" in lines[2]


def test_main_prints_table_and_decisions(tmp_path, monkeypatch, capsys):
    write_results(tmp_path, "old", CURRENT)
    write_results(tmp_path, "good", summary(perfect_pos=10))
    write_results(tmp_path, "pricey", summary(cost_per_po_usd=0.01))
    monkeypatch.setattr(compare_extraction, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        compare_extraction,
        "load_summaries",
        lambda model: load_summaries(model, tmp_path),
    )

    assert main(["old", "good", "pricey"]) == {"good": True, "pricey": False}
    out = capsys.readouterr().out
    assert "good PASSES" in out
    assert "pricey fails: poc/data: cost per PO +400%" in out


@pytest.mark.parametrize("missing", ["data", "train-first20", "holdout"])
def test_missing_results_file_raises(tmp_path, missing):
    write_results(tmp_path, "m", CURRENT)
    (tmp_path / f"{missing}-m.json").unlink()

    with pytest.raises(FileNotFoundError):
        load_summaries("m", tmp_path)
