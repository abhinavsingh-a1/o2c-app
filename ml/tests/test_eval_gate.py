import json

import eval_gate
import pytest
from eval_gate import (
    above,
    at_least,
    at_most,
    below,
    format_markdown,
    format_table,
    load_thresholds,
    main,
    plan,
    run_gate,
    save_gate_result,
    write_report,
)
from litellm_common import CostMeter

THRESHOLDS = load_thresholds()


def extraction_summary(perfect: int, documents: int, accuracy: float) -> dict:
    return {
        "documents": documents,
        "perfect_pos": perfect,
        "field_accuracy": accuracy,
        "top_errors": [] if perfect == documents else [{"error": "x", "count": 1}],
    }


@pytest.fixture
def fake_runs(monkeypatch):
    """Recorded results at the baselines; tests change them to fail a check. Each run
    adds a cost to the meter, as LiteLLM's headers would."""
    meter = CostMeter()
    calls = []

    def costs(name, value):
        calls.append(name)
        meter.calls += 1
        meter.cost_usd += 0.01
        return value

    state = {
        "extraction": {
            "poc_data": extraction_summary(50, 50, 1.0),
            "holdout": extraction_summary(9, 20, 0.9901),
        },
        "classifier": {"accuracy": 1.0, "false_purchase_orders": 0, "mistakes": []},
        "calibration": [{"id": f"c{i}", "ok": True} for i in range(7)],
        "explanations": [
            {"dataset": "poc/data", "po_id": f"PO-{i}", "passed": True}
            for i in range(14)
        ],
        "redteam": [{"case": f"RT-0{i}", "passed": True} for i in range(1, 9)],
        "injection": {"checked": 200, "flagged": {}},
        "anomaly": {
            "model": "o2c-quantity-anomaly v3",
            "line": "1000101/400101 median 40",
            "normal": 0.31,
            "outlier": 0.97,
            "errors": [],
        },
    }

    def run_anomaly_scores(outlier_factor):
        calls.append(f"anomaly x{outlier_factor:g}")  # MLflow only: no LLM cost
        return state["anomaly"]

    def run_extraction(dataset, limit):
        if limit:
            return costs("extraction quick", extraction_summary(limit, limit, 1.0))
        key = "holdout" if dataset == eval_gate.HOLDOUT else "poc_data"
        return costs(f"extraction {key}", state["extraction"][key])

    monkeypatch.setattr(eval_gate, "run_extraction", run_extraction)
    monkeypatch.setattr(
        eval_gate, "run_classifier", lambda: costs("classifier", state["classifier"])
    )
    monkeypatch.setattr(
        eval_gate,
        "run_judge",
        lambda: costs("judge", (state["calibration"], state["explanations"])),
    )
    monkeypatch.setattr(
        eval_gate, "run_redteam_cases", lambda: costs("redteam", state["redteam"])
    )
    monkeypatch.setattr(
        eval_gate, "injection_findings", lambda: costs("injection", state["injection"])
    )
    monkeypatch.setattr(eval_gate, "run_anomaly_scores", run_anomaly_scores)
    return state, meter, calls


def by_name(report: dict) -> dict[str, dict]:
    return {c["name"]: c for c in report["checks"]}


def test_thresholds_file_has_the_agreed_values():
    t = THRESHOLDS
    assert t["extraction"]["poc_data"] == {
        "perfect_pos_min": 48,
        "field_accuracy_min": 0.995,
    }
    assert t["extraction"]["holdout"] == {
        "perfect_pos_min": 9,
        "field_accuracy_min": 0.988,
    }
    assert t["extraction"]["poc_data_quick"]["limit"] == 10
    assert t["classifier"] == {"accuracy_min": 0.975, "false_purchase_orders_max": 0}
    assert t["judge"] == {"calibration_correct_min": 7, "pass_rate_min": 0.9}
    assert t["redteam"] == {"passed_min": 8}
    assert t["injection"] == {"false_positives_max": 0}
    assert t["anomaly"] == {
        "normal_score_max": 0.8,
        "outlier_factor": 20,
        "outlier_score_min": 0.8,
        "scoring_errors_max": 0,
    }


def test_at_least_and_at_most():
    assert at_least("s", "n", 48, 48).passed
    assert not at_least("s", "n", 47, 48).passed
    assert at_most("s", "n", 0, 0).passed
    check = at_most("s", "n", 1, 0, detail="PO-1")
    assert (check.passed, check.shown_actual, check.shown_threshold) == (
        False,
        "1",
        "<= 0",
    )


def test_below_and_above_are_strict_and_fail_without_a_value():
    assert below("s", "n", 0.79, 0.8).passed
    assert not below("s", "n", 0.8, 0.8).passed
    assert above("s", "n", 0.81, 0.8).passed
    assert not above("s", "n", 0.8, 0.8).passed
    check = above("s", "n", None, 0.8, lambda v: f"{v:.3f}")
    assert (check.passed, check.shown_actual, check.shown_threshold) == (
        False,
        "none",
        "> 0.800",
    )


def test_quick_runs_extraction_first_10_red_team_and_injection():
    steps = [name for name, _ in plan("quick", THRESHOLDS)]
    assert steps == [
        "extraction poc/data (first 10)",
        "anomaly model scores",
        "red team",
        "injection detector",
    ]


def test_ci_runs_extraction_first_10_and_injection_only():
    steps = [name for name, _ in plan("ci", THRESHOLDS)]
    assert steps == ["extraction poc/data (first 10)", "injection detector"]


def test_ci_mode_from_the_command_line(fake_runs, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        eval_gate, "write_report", lambda report: write_report(report, tmp_path)
    )

    assert main(["--ci"]) == 0

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "ci"
    assert [s["step"] for s in report["steps"]] == [
        "extraction poc/data (first 10)",
        "injection detector",
    ]


def test_full_runs_everything():
    steps = [name for name, _ in plan("full", THRESHOLDS)]
    assert steps == [
        "extraction poc/data",
        "extraction holdout",
        "hybrid classifier",
        "judge",
        "anomaly model scores",
        "red team",
        "injection detector",
    ]


def test_full_gate_passes_at_the_baselines(fake_runs):
    _, meter, calls = fake_runs

    report = run_gate("full", THRESHOLDS, meter)

    assert report["passed"]
    assert len(report["checks"]) == 13
    assert calls == [
        "extraction poc_data",
        "extraction holdout",
        "classifier",
        "judge",
        "anomaly x20",
        "redteam",
        "injection",
    ]
    assert report["cost_usd"] == pytest.approx(0.06)
    assert [s["cost_usd"] for s in report["steps"]] == [0.01] * 4 + [0.0] + [0.01] * 2
    checks = by_name(report)
    assert checks["anomaly model: 20x line score"]["shown_actual"] == "0.970"
    assert checks["anomaly model: normal line score"]["shown_threshold"] == "< 0.800"
    assert checks["poc/data: perfect POs"]["shown_actual"] == "50/50"
    assert checks["poc/data: perfect POs"]["shown_threshold"] == ">= 48/50"
    assert checks["holdout: field accuracy"]["shown_actual"] == "99.01%"
    assert checks["explanation pass rate (14/14)"]["passed"]


def test_quick_gate_uses_the_first_10_pos(fake_runs):
    _, meter, calls = fake_runs

    report = run_gate("quick", THRESHOLDS, meter)

    assert report["passed"]
    assert calls == ["extraction quick", "anomaly x20", "redteam", "injection"]
    assert by_name(report)["poc/data first 10: perfect POs"]["shown_actual"] == "10/10"


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        (
            lambda s: s["extraction"].update(
                poc_data=extraction_summary(47, 50, 0.999)
            ),
            "poc/data: perfect POs",
        ),
        (
            lambda s: s["extraction"].update(holdout=extraction_summary(9, 20, 0.987)),
            "holdout: field accuracy",
        ),
        (
            lambda s: s["classifier"].update(accuracy=0.95),
            "hybrid holdout: accuracy",
        ),
        (
            lambda s: s["classifier"].update(false_purchase_orders=1),
            "hybrid holdout: false purchase_order labels",
        ),
        (
            lambda s: s["redteam"][4].update(passed=False),
            "red-team cases passed",
        ),
        (
            lambda s: s["anomaly"].update(normal=0.85),
            "anomaly model: normal line score",
        ),
        (
            lambda s: s["anomaly"].update(outlier=0.62),
            "anomaly model: 20x line score",
        ),
        (
            lambda s: s["injection"]["flagged"].update({"poc/data/pos/PO-1.pdf": []}),
            "injection false positives (200 documents)",
        ),
    ],
)
def test_one_failed_threshold_fails_the_gate(fake_runs, change, failing):
    state, meter, _ = fake_runs
    change(state)

    report = run_gate("full", THRESHOLDS, meter)

    assert not report["passed"]
    assert [c["name"] for c in report["checks"] if not c["passed"]] == [failing]


def test_explanation_pass_rate_below_90_percent_fails(fake_runs):
    state, meter, _ = fake_runs
    for row in state["explanations"][:2]:
        row["passed"] = False  # 12/14 = 85.7%

    checks = by_name(run_gate("full", THRESHOLDS, meter))

    check = checks["explanation pass rate (12/14)"]
    assert not check["passed"]
    assert check["detail"] == "poc/data PO-0, poc/data PO-1"


def test_an_explanation_error_counts_as_not_passed(fake_runs):
    state, meter, _ = fake_runs
    state["explanations"][0].update(passed=False, status="error")

    checks = by_name(run_gate("full", THRESHOLDS, meter))

    check = checks["explanation pass rate (13/14)"]
    assert check["passed"]  # 92.9% >= 90%
    assert check["detail"] == "poc/data PO-0 (error)"


def test_a_wrong_judge_fails_and_explanations_are_not_judged(fake_runs):
    state, meter, _ = fake_runs
    state["calibration"][3]["ok"] = False
    state["explanations"] = []

    checks = by_name(run_gate("full", THRESHOLDS, meter))

    assert checks["judge validation: correct verdicts"]["shown_actual"] == "6/7"
    assert checks["explanation pass rate"]["shown_actual"] == "not judged"
    assert not checks["explanation pass rate"]["passed"]


def test_the_pandas_3_fallback_fails_every_anomaly_check(fake_runs):
    # What the agent hid with its 0.0 fallback: both lines raise in schema enforcement.
    state, meter, _ = fake_runs
    error = "MlflowException: Incompatible input types for column customer_number"
    state["anomaly"].update(
        normal=None, outlier=None, errors=[f"normal: {error}", f"outlier: {error}"]
    )

    checks = by_name(run_gate("quick", THRESHOLDS, meter))

    failed = [name for name, c in checks.items() if not c["passed"]]
    assert failed == [
        "anomaly model: normal line score",
        "anomaly model: 20x line score",
        "anomaly model: scoring errors",
    ]
    assert checks["anomaly model: scoring errors"]["shown_actual"] == "2"
    assert "customer_number" in checks["anomaly model: scoring errors"]["detail"]


def test_a_step_that_raises_fails_the_gate_and_the_others_still_run(
    fake_runs, monkeypatch
):
    _, meter, calls = fake_runs

    def mlflow_down():
        raise ConnectionError("MLflow unavailable")

    monkeypatch.setattr(eval_gate, "run_classifier", mlflow_down)

    report = run_gate("full", THRESHOLDS, meter)

    assert not report["passed"]
    failed = [c for c in report["checks"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["hybrid classifier ran"]
    assert failed[0]["detail"] == "ConnectionError: MLflow unavailable"
    assert "redteam" in calls and "injection" in calls
    assert report["steps"][2]["error"] == "ConnectionError: MLflow unavailable"


def test_table_and_markdown(fake_runs):
    _, meter, _ = fake_runs
    report = {
        "started_at": "2026-10-22T10:00:00+00:00",
        "git_sha": "abc1234",
        **run_gate("quick", THRESHOLDS, meter),
    }

    table = format_table(report["checks"]).splitlines()
    assert table[0].split() == ["check", "actual", "threshold", "result"]
    assert table[2].startswith("poc/data first 10: perfect POs")
    assert table[2].endswith("PASS")

    md = format_markdown(report)
    assert md.startswith("# Evaluation gate: PASS")
    assert "- OpenAI cost: $0.0300 (3 LLM calls)" in md
    assert "- LiteLLM fallbacks: 0" in md
    assert "| red-team cases passed | 8/8 | >= 8/8 | PASS |  |" in md


def test_write_report(tmp_path, fake_runs):
    _, meter, _ = fake_runs
    report = {"started_at": "t", "git_sha": "s", **run_gate("quick", THRESHOLDS, meter)}

    md, js = write_report(report, tmp_path / "eval")

    assert md.read_text(encoding="utf-8").startswith("# Evaluation gate: PASS")
    assert json.loads(js.read_text(encoding="utf-8"))["mode"] == "quick"


def test_main_exits_1_on_a_failure(fake_runs, monkeypatch, tmp_path, capsys):
    state, _, _ = fake_runs
    monkeypatch.setattr(eval_gate, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(
        eval_gate,
        "write_report",
        lambda report: write_report(report, tmp_path),
    )

    assert main(["--quick"]) == 0
    out = capsys.readouterr().out
    assert "gate: PASS" in out
    assert "OpenAI cost of this run: $" in out

    state["redteam"][0]["passed"] = False
    assert main(["--quick"]) == 1
    assert "gate: FAIL" in capsys.readouterr().out
    assert (tmp_path / "report.json").exists()


def test_main_needs_a_mode():
    with pytest.raises(SystemExit):
        main([])


def test_full_runs_are_saved_per_o2c_extract_model(tmp_path, fake_runs):
    _, meter, _ = fake_runs
    meter.fallbacks = 2
    full = {"o2c_extract": "openai/gpt-6-luna", **run_gate("full", THRESHOLDS, meter)}
    quick = {"o2c_extract": "openai/gpt-6-luna", **run_gate("quick", THRESHOLDS, meter)}

    path = save_gate_result(full, tmp_path)

    assert path == tmp_path / "gate-openai-gpt-6-luna.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (saved["mode"], saved["llm_fallbacks"]) == ("full", 2)
    assert save_gate_result(quick, tmp_path) is None
    assert "- o2c-extract: openai/gpt-6-luna" in format_markdown(
        {"started_at": "t", "git_sha": "s", **full}
    )


def test_run_anomaly_scores_loads_the_champion_and_scores_without_fallback(
    tmp_path, monkeypatch
):
    # A real (file) MLflow registry with a signed champion, as ml/train_anomaly.py logs it.
    import mlflow
    import numpy as np
    import pandas as pd
    from agent.anomaly_model import train
    from mlflow import MlflowClient
    from mlflow.models import infer_signature

    rng = np.random.default_rng(3)
    rows = [("1000101", "400101", float(round(rng.normal(40, 8)))) for _ in range(60)]
    rows += [("1000101", "400102", float(round(rng.normal(5, 1)))) for _ in range(20)]
    rows += [("1000101", "400101", 800.0), ("1000101", "400102", 120.0)]
    history = pd.DataFrame(
        rows, columns=["customer_number", "material_number", "quantity"]
    )
    model, _ = train(history, np.array([False] * 80 + [True, True]))
    uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    # set_tracking_uri is global: restore it for the other tests
    monkeypatch.setattr(mlflow.tracking._tracking_service.utils, "_tracking_uri", None)
    mlflow.set_tracking_uri(uri)
    example = history.head(3)
    with mlflow.start_run():
        mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=model,
            input_example=example,
            signature=infer_signature(example, model.score(example)),
            registered_model_name="o2c-quantity-anomaly",
        )
    MlflowClient().set_registered_model_alias("o2c-quantity-anomaly", "champion", "1")

    r = eval_gate.run_anomaly_scores(20)

    assert r["errors"] == []
    assert r["model"] == "o2c-quantity-anomaly v1"
    assert r["line"].startswith("1000101/400101 median ")  # most history lines
    assert 0.0 < r["normal"] < 0.8 < r["outlier"] <= 1.0
