import json

import compare_extraction
import pytest
from compare_extraction import (
    DATASETS,
    RUNS,
    best_candidate,
    compare_dataset,
    format_table,
    gate_failures,
    load_summaries,
    main,
    mean_summary,
    should_migrate,
    table_rows,
)

CURRENT = {
    "runs": 3,
    "documents": 20,
    "perfect_pos": 9.0,
    "perfect_min": 9,
    "perfect_max": 9,
    "field_accuracy": 0.9901,
    "avg_latency_s": 4.0,
    "cost_per_po_usd": 0.002,
    "fallbacks": 0,
}


def summary(**changes) -> dict:
    return {**CURRENT, **changes}


def everywhere(s: dict) -> dict[str, dict]:
    return {label: s for label, _, _ in DATASETS}


def run_summary(perfect: int, accuracy: float, latency=4.0, cost=0.002) -> dict:
    """One eval_extraction summary."""
    return {
        "documents": 20,
        "perfect_pos": perfect,
        "field_accuracy": accuracy,
        "avg_latency_s": latency,
        "cost_per_po_usd": cost,
        "fallbacks": 0,
    }


def test_mean_summary_averages_the_runs():
    s = mean_summary(
        [
            run_summary(8, 0.99, latency=3.0, cost=0.001),
            run_summary(10, 0.995, latency=5.0, cost=None),
            run_summary(9, 0.992, latency=4.0, cost=0.003),
        ]
    )

    assert s["runs"] == 3
    assert s["perfect_pos"] == 9.0
    assert (s["perfect_min"], s["perfect_max"]) == (8, 10)
    assert s["field_accuracy"] == pytest.approx(0.9923333)
    assert s["avg_latency_s"] == 4.0
    assert s["cost_per_po_usd"] == 0.002  # runs without cost are left out


def test_better_candidate_passes():
    candidate = summary(perfect_pos=12.0, field_accuracy=0.995)
    assert compare_dataset(CURRENT, candidate, repeated=True) == []


def test_mean_perfect_pos_may_be_one_lower():
    assert compare_dataset(CURRENT, summary(perfect_pos=8.0), repeated=True) == []
    assert compare_dataset(CURRENT, summary(perfect_pos=7.67), repeated=True) == [
        "perfect POs 7.67 < 9.00 - 1"
    ]


def test_field_accuracy_may_drop_at_most_0_2_points_on_repeated_datasets():
    assert compare_dataset(CURRENT, summary(field_accuracy=0.9881), True) == []
    assert compare_dataset(CURRENT, summary(field_accuracy=0.9880), True) == [
        "field accuracy 0.21 points lower"
    ]


def test_holdout_checks_no_field_accuracy():
    assert compare_dataset(CURRENT, summary(field_accuracy=0.95), repeated=False) == []


def test_cost_may_rise_at_most_50_percent():
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.003), True) == []
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.0031), True) == [
        "cost per PO +55%"
    ]
    assert compare_dataset(CURRENT, summary(cost_per_po_usd=0.0001), True) == []


def test_latency_may_be_at_most_2_5_times():
    assert compare_dataset(CURRENT, summary(avg_latency_s=10.0), True) == []
    assert compare_dataset(CURRENT, summary(avg_latency_s=10.4), True) == [
        "latency 2.6x"
    ]


def test_unmeasured_cost_and_latency_fail():
    candidate = summary(cost_per_po_usd=None, avg_latency_s=None)
    assert compare_dataset(CURRENT, candidate, True) == [
        "cost per PO not measured",
        "latency not measured",
    ]


def test_should_migrate_needs_every_dataset():
    candidate = everywhere(summary(perfect_pos=10.0))
    assert should_migrate(everywhere(CURRENT), candidate) == (True, [])

    candidate["holdout"] = summary(perfect_pos=7.0)
    ok, reasons = should_migrate(everywhere(CURRENT), candidate)
    assert not ok
    assert reasons == ["holdout: perfect POs 7.00 < 9.00 - 1"]


def test_should_migrate_fails_without_a_holdout_result():
    candidate = {**everywhere(CURRENT), "holdout": None}

    assert should_migrate(everywhere(CURRENT), candidate) == (
        False,
        ["holdout: not measured"],
    )


def test_best_candidate_has_most_perfect_pos_then_lowest_cost():
    summaries = {
        "a": everywhere(summary(perfect_pos=9.0, cost_per_po_usd=0.001)),
        "b": everywhere(summary(perfect_pos=10.0, cost_per_po_usd=0.002)),
        "c": everywhere(summary(perfect_pos=10.0, cost_per_po_usd=0.0015)),
        "d": everywhere(summary(perfect_pos=20.0)),
    }
    decisions = {"a": True, "b": True, "c": True, "d": False}

    assert best_candidate(decisions, summaries) == "c"
    assert best_candidate({"d": False}, summaries) is None


def write_results(results_dir, model: str, *runs: dict, holdout: dict | None = None):
    for _, prefix, repeated in DATASETS:
        if repeated:
            for i, s in enumerate(runs, start=1):
                (results_dir / f"{prefix}-{model}-run{i}.json").write_text(
                    json.dumps({"summary": s}), encoding="utf-8"
                )
        elif holdout is not None:
            (results_dir / f"{prefix}-{model}.json").write_text(
                json.dumps({"summary": holdout}), encoding="utf-8"
            )


def three_runs(**kw) -> list[dict]:
    return [run_summary(**kw)] * RUNS


def test_load_summaries_averages_repeated_runs(tmp_path):
    write_results(
        tmp_path,
        "m",
        run_summary(8, 0.99),
        run_summary(9, 0.99),
        run_summary(10, 0.99),
        holdout=run_summary(9, 0.98),
    )

    summaries = load_summaries("m", tmp_path)

    assert summaries["poc/data"]["perfect_pos"] == 9.0
    assert summaries["poc/data"]["runs"] == 3
    assert summaries["holdout"]["runs"] == 1
    assert summaries["holdout"]["field_accuracy"] == 0.98


def test_load_summaries_without_holdout(tmp_path):
    write_results(tmp_path, "m", *three_runs(perfect=9, accuracy=0.99))

    assert load_summaries("m", tmp_path)["holdout"] is None


@pytest.mark.parametrize("missing", ["data-m-run3", "train-first20-m-run1"])
def test_missing_repeated_run_raises(tmp_path, missing):
    write_results(tmp_path, "m", *three_runs(perfect=9, accuracy=0.99))
    (tmp_path / f"{missing}.json").unlink()

    with pytest.raises(FileNotFoundError):
        load_summaries("m", tmp_path)


def test_table_rows_and_format():
    rows = table_rows(
        {
            "o2c-extract": {
                "poc/data": summary(perfect_pos=8.666, perfect_min=8, perfect_max=9),
                "holdout": None,
            }
        }
    )

    assert rows[0] == [
        "o2c-extract",
        "poc/data",
        "3",
        "8.67/20",
        "8-9",
        "99.01%",
        "4.0 s",
        "$0.00200",
        "0",
    ]
    assert rows[1][:3] == ["o2c-extract", "holdout", "0"]
    lines = format_table(rows).splitlines()
    assert lines[0].startswith("model")
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert "99.01%" in lines[2]


def test_main_prints_table_decisions_and_best(tmp_path, monkeypatch, capsys):
    holdout = run_summary(9, 0.99)
    write_results(
        tmp_path, "old", *three_runs(perfect=9, accuracy=0.99), holdout=holdout
    )
    write_results(
        tmp_path, "good", *three_runs(perfect=10, accuracy=0.99), holdout=holdout
    )
    write_results(
        tmp_path,
        "pricey",
        *three_runs(perfect=10, accuracy=0.99, cost=0.01),
        holdout=holdout,
    )
    write_results(tmp_path, "untested", *three_runs(perfect=9, accuracy=0.99))
    write_results(
        tmp_path, "gateless", *three_runs(perfect=10, accuracy=0.99), holdout=holdout
    )
    monkeypatch.setattr(
        compare_extraction,
        "load_summaries",
        lambda model: load_summaries(model, tmp_path),
    )
    monkeypatch.setattr(
        compare_extraction,
        "gate_failures",
        lambda model: ["full gate: not run"] if model == "gateless" else [],
    )

    decisions = main(["old", "good", "pricey", "untested", "gateless"])

    assert decisions == {
        "good": True,
        "pricey": False,
        "untested": False,
        "gateless": False,
    }
    out = capsys.readouterr().out
    assert "good PASSES" in out
    assert "pricey fails: poc/data: cost per PO +400%" in out
    assert "untested fails: holdout: not measured" in out
    assert "gateless fails: full gate: not run" in out
    assert "best passing candidate: good" in out


def gate_report(passed=True, fallbacks=0) -> dict:
    return {
        "mode": "full",
        "llm_fallbacks": fallbacks,
        "checks": [
            {"name": "red-team cases passed", "shown_actual": "8/8", "passed": True},
            {
                "name": "explanations: pass rate",
                "shown_actual": "80.0%",
                "passed": passed,
            },
        ],
    }


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, ["full gate: not run with o2c-extract -> openai/gpt-x (make eval)"]),
        (gate_report(), []),
        (gate_report(passed=False), ["full gate: explanations: pass rate 80.0%"]),
        (gate_report(fallbacks=3), ["full gate: 3 calls answered by a fallback"]),
    ],
)
def test_gate_failures(tmp_path, monkeypatch, report, expected):
    monkeypatch.setattr(
        compare_extraction, "alias_target", lambda alias: "openai/gpt-x"
    )
    if report is not None:
        (tmp_path / "gate-openai-gpt-x.json").write_text(
            json.dumps(report), encoding="utf-8"
        )

    assert gate_failures("candidate", tmp_path) == expected
