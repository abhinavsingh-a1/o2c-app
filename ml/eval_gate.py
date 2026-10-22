"""Evaluation quality gate (E11 Part 3, ADR-010).

Usage: uv run --group ml python ml/eval_gate.py --quick | --full
  make eval-quick / make eval

Runs the evaluations and compares each result with the absolute thresholds in
eval/thresholds.yaml:
  --quick  extraction on the first 10 POs of poc/data, red team, injection false
           positives (a few minutes, about one cent; unit tests are not included:
           run uv run pytest for those)
  --full   extraction on poc/data (50) and ml/data/holdout (20), hybrid classifier on
           ml/data/classifier/holdout, judge validation and explanation pass rate
           (poc/data and holdout), red team, injection false positives

Needs the local stack (make up): LiteLLM, the ERP mock, the contract index and, for
--full, MLflow with o2c-doc-classifier@champion. LiteLLM is called with LITELLM_KEY_EVAL
when it is set (ml/litellm_common.py). The holdout is only measured here, never used to
tune anything.

Prints a PASS/FAIL table and the OpenAI cost of the run (the sum of LiteLLM's
x-litellm-response-cost over every call), writes eval/report.md and eval/report.json
(git-ignored) and exits 1 if any check fails. A step that cannot run (e.g. MLflow down)
fails the gate.

The report names the model behind o2c-extract (local/litellm/config.yaml) and counts the
calls LiteLLM answered through a fallback. A --full run is also saved as
ml/results/gate-<model>.json: the migration rule (ml/compare_extraction.py, ADR-008)
requires a passing full gate without fallbacks for a candidate, run with o2c-extract
temporarily pointed at it, because every LLM call site (classifier, extraction, tolerance,
material matching, explanations) uses o2c-extract.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ML_DIR = REPO_ROOT / "ml"
RESULTS_DIR = ML_DIR / "results"
EVAL_DIR = REPO_ROOT / "eval"
THRESHOLDS = EVAL_DIR / "thresholds.yaml"
POC_DATA = REPO_ROOT / "poc" / "data"
HOLDOUT = ML_DIR / "data" / "holdout"
# Every real PO and document (the injection detector must not flag any of them)
INJECTION_DIRS = [
    POC_DATA / "pos",
    POC_DATA / "other",
    ML_DIR / "data" / "train" / "pos",
    HOLDOUT / "pos",
]
MODEL = "o2c-extract"


@dataclass
class Check:
    step: str
    name: str
    actual: float | int | None
    op: str  # ">=" or "<="
    threshold: float | int
    shown_actual: str
    shown_threshold: str
    passed: bool
    detail: str = ""


def at_least(step, name, actual, minimum, fmt=str, detail="") -> Check:
    return Check(
        step,
        name,
        actual,
        ">=",
        minimum,
        fmt(actual),
        ">= " + fmt(minimum),
        actual >= minimum,
        detail,
    )


def at_most(step, name, actual, maximum, fmt=str, detail="") -> Check:
    return Check(
        step,
        name,
        actual,
        "<=",
        maximum,
        fmt(actual),
        "<= " + fmt(maximum),
        actual <= maximum,
        detail,
    )


def pct(value: float) -> str:
    return f"{value:.2%}"


def out_of(total: int) -> Callable[[int | float], str]:
    return lambda value: f"{value}/{total}"


# --- the evaluations (real runs; replaced in the tests) -----------------------------


def run_extraction(dataset: Path, limit: int | None) -> dict:
    """eval_extraction summary of o2c-extract on the dataset (first `limit` POs)."""
    from eval_extraction import ResponseRecorder, evaluate, make_settings, summarize
    from eval_extraction import format_row as fmt
    from extractor.app import make_extractor

    recorder = ResponseRecorder()
    extract = make_extractor(make_settings(), MODEL, http_client=recorder.client())
    rows = evaluate(
        dataset,
        extract,
        progress=lambda row: print("  " + fmt(row), flush=True),
        limit=limit,
        recorder=recorder,
    )
    return summarize(rows)


def run_classifier() -> dict:
    """Hybrid classifier summary on ml/data/classifier/holdout (ml/eval_hybrid.py)."""
    from classifier.app import DEFAULT_NON_PO_THRESHOLD
    from eval_hybrid import HOLDOUT as CLASSIFIER_HOLDOUT
    from eval_hybrid import evaluate, make_classify_hybrid, summarize_hybrid

    rows = evaluate(CLASSIFIER_HOLDOUT, make_classify_hybrid(DEFAULT_NON_PO_THRESHOLD))
    return summarize_hybrid(rows)


def run_judge() -> tuple[list, list]:
    """(judge validation rows, judged explanation rows of poc/data and holdout); the
    explanations are not judged if the judge fails its validation."""
    from judge_explanations import (
        calibrate,
        judge_agent_explanations,
        load_calibration,
        make_judge,
    )

    judge = make_judge()
    calibration = calibrate(load_calibration(), judge)
    if not all(r["ok"] for r in calibration):
        return calibration, []
    return calibration, judge_agent_explanations(judge, None, None)


def run_redteam_cases() -> list[dict]:
    from eval_redteam import format_result, run_redteam

    return run_redteam(progress=lambda r: print("  " + format_result(r), flush=True))


def injection_findings() -> dict[str, list]:
    """document -> findings, for every real document that has any."""
    from o2c_common.injection import detect
    from o2c_common.pdf import pdf_text

    flagged, checked = {}, 0
    for directory in INJECTION_DIRS:
        for pdf in sorted(directory.glob("*.pdf")):
            checked += 1
            findings = detect(pdf_text(pdf.read_bytes()))
            if findings:
                flagged[pdf.relative_to(REPO_ROOT).as_posix()] = [
                    f.to_dict() for f in findings
                ]
    return {"checked": checked, "flagged": flagged}


# --- steps: run an evaluation, compare with the thresholds --------------------------


def extraction_step(label: str, dataset: Path, t: dict) -> list[Check]:
    s = run_extraction(dataset, t.get("limit"))
    n = s["documents"]
    errors = ", ".join(f"{e['error']} x{e['count']}" for e in s["top_errors"])
    return [
        at_least(
            "extraction",
            f"{label}: perfect POs",
            s["perfect_pos"],
            t["perfect_pos_min"],
            out_of(n),
            errors,
        ),
        at_least(
            "extraction",
            f"{label}: field accuracy",
            s["field_accuracy"],
            t["field_accuracy_min"],
            pct,
        ),
    ]


def classifier_step(t: dict) -> list[Check]:
    s = run_classifier()
    mistakes = ", ".join(
        f"{m['file']} {m['expected']}->{m['predicted']}" for m in s["mistakes"]
    )
    return [
        at_least(
            "classifier",
            "hybrid holdout: accuracy",
            s["accuracy"],
            t["accuracy_min"],
            pct,
            mistakes,
        ),
        at_most(
            "classifier",
            "hybrid holdout: false purchase_order labels",
            s["false_purchase_orders"],
            t["false_purchase_orders_max"],
        ),
    ]


def judge_step(t: dict) -> list[Check]:
    calibration, explanations = run_judge()
    correct = sum(r["ok"] for r in calibration)
    wrong = ", ".join(r["id"] for r in calibration if not r["ok"])
    checks = [
        at_least(
            "judge",
            "judge validation: correct verdicts",
            correct,
            t["calibration_correct_min"],
            out_of(len(calibration)),
            wrong,
        )
    ]
    if not explanations:
        checks.append(
            Check(
                "judge",
                "explanation pass rate",
                None,
                ">=",
                t["pass_rate_min"],
                "not judged",
                ">= " + pct(t["pass_rate_min"]),
                False,
                "judge validation failed" if wrong else "no explanations",
            )
        )
        return checks
    passed = sum(r["passed"] for r in explanations)
    failed = ", ".join(
        f"{r['dataset']} {r['po_id']}" for r in explanations if not r["passed"]
    )
    checks.append(
        at_least(
            "judge",
            f"explanation pass rate ({passed}/{len(explanations)})",
            round(passed / len(explanations), 4),
            t["pass_rate_min"],
            pct,
            failed,
        )
    )
    return checks


def redteam_step(t: dict) -> list[Check]:
    results = run_redteam_cases()
    passed = sum(r["passed"] for r in results)
    failed = ", ".join(r["case"] for r in results if not r["passed"])
    return [
        at_least(
            "redteam",
            "red-team cases passed",
            passed,
            t["passed_min"],
            out_of(len(results)),
            failed,
        )
    ]


def injection_step(t: dict) -> list[Check]:
    result = injection_findings()
    flagged = result["flagged"]
    return [
        at_most(
            "injection",
            f"injection false positives ({result['checked']} documents)",
            len(flagged),
            t["false_positives_max"],
            detail=", ".join(flagged),
        )
    ]


def plan(mode: str, thresholds: dict) -> list[tuple[str, Callable[[], list[Check]]]]:
    """The (step, run) pairs of a mode."""
    ex = thresholds["extraction"]
    steps = []
    if mode == "quick":
        steps.append(
            (
                "extraction poc/data (first 10)",
                lambda: extraction_step(
                    f"poc/data first {ex['poc_data_quick']['limit']}",
                    POC_DATA,
                    ex["poc_data_quick"],
                ),
            )
        )
    else:
        steps += [
            (
                "extraction poc/data",
                lambda: extraction_step("poc/data", POC_DATA, ex["poc_data"]),
            ),
            (
                "extraction holdout",
                lambda: extraction_step("holdout", HOLDOUT, ex["holdout"]),
            ),
            ("hybrid classifier", lambda: classifier_step(thresholds["classifier"])),
            ("judge", lambda: judge_step(thresholds["judge"])),
        ]
    steps += [
        ("red team", lambda: redteam_step(thresholds["redteam"])),
        ("injection detector", lambda: injection_step(thresholds["injection"])),
    ]
    return steps


def run_gate(mode: str, thresholds: dict, meter) -> dict:
    """Run every step of the mode; a step that raises fails the gate."""
    checks, steps = [], []
    for name, run in plan(mode, thresholds):
        print(f"== {name}", flush=True)
        cost_before, start = meter.cost_usd, time.perf_counter()
        try:
            step_checks = run()
            error = None
        except Exception as exc:  # noqa: BLE001 - report it as a failed check
            error = f"{type(exc).__name__}: {exc}"
            step_checks = [
                Check(name, f"{name} ran", None, "", 0, "error", "runs", False, error)
            ]
        checks += step_checks
        steps.append(
            {
                "step": name,
                "seconds": round(time.perf_counter() - start, 1),
                "cost_usd": round(meter.cost_usd - cost_before, 6),
                "error": error,
            }
        )
    return {
        "mode": mode,
        "passed": all(c.passed for c in checks),
        "cost_usd": round(meter.cost_usd, 6),
        "llm_calls": meter.calls,
        "llm_fallbacks": meter.fallbacks,
        "steps": steps,
        "checks": [asdict(c) for c in checks],
    }


def format_table(checks: list[dict]) -> str:
    header = ["check", "actual", "threshold", "result"]
    rows = [
        [c["name"], c["shown_actual"], c["shown_threshold"], result(c)] for c in checks
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip()
        for r in [header, *rows]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def result(check: dict) -> str:
    return "PASS" if check["passed"] else "FAIL"


def format_markdown(report: dict) -> str:
    lines = [
        f"# Evaluation gate: {'PASS' if report['passed'] else 'FAIL'}",
        "",
        f"- Mode: {report['mode']}",
        f"- Run: {report['started_at']} (git {report['git_sha']})",
        f"- OpenAI cost: ${report['cost_usd']:.4f} ({report['llm_calls']} LLM calls)",
        f"- o2c-extract: {report.get('o2c_extract', 'unknown')}",
        f"- LiteLLM fallbacks: {report.get('llm_fallbacks', 0)}",
        "- Thresholds: eval/thresholds.yaml",
        "",
        "| Check | Actual | Threshold | Result | Detail |",
        "|---|---|---|---|---|",
    ]
    for c in report["checks"]:
        detail = c["detail"].replace("|", "\\|")
        lines.append(
            f"| {c['name']} | {c['shown_actual']} | {c['shown_threshold']} "
            f"| {result(c)} | {detail} |"
        )
    lines += ["", "| Step | Seconds | Cost |", "|---|---|---|"]
    for s in report["steps"]:
        lines.append(f"| {s['step']} | {s['seconds']} | ${s['cost_usd']:.4f} |")
    return "\n".join(lines) + "\n"


def write_report(report: dict, directory: Path = EVAL_DIR) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    md, js = directory / "report.md", directory / "report.json"
    md.write_text(format_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return md, js


def gate_result_path(target: str, results_dir: Path = RESULTS_DIR) -> Path:
    from litellm_common import target_slug

    return results_dir / f"gate-{target_slug(target)}.json"


def save_gate_result(report: dict, results_dir: Path = RESULTS_DIR) -> Path | None:
    """Keep a full run per o2c-extract model for the migration rule; None for --quick."""
    if report["mode"] != "full" or not report.get("o2c_extract"):
        return None
    path = gate_result_path(report["o2c_extract"], results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def load_thresholds(path: Path = THRESHOLDS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    from langfuse_common import git_short_sha
    from litellm_common import CostMeter, alias_target, use_eval_key

    parser = argparse.ArgumentParser(description="Evaluation quality gate.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--quick", action="store_true")
    group.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    mode = "quick" if args.quick else "full"

    using_eval_key = use_eval_key()
    target = alias_target(MODEL)
    print(
        f"evaluation gate ({mode}), {MODEL} -> {target}, LiteLLM key: "
        f"{'LITELLM_KEY_EVAL' if using_eval_key else 'LITELLM_MASTER_KEY'}"
    )
    started = datetime.now(UTC).isoformat(timespec="seconds")
    meter = CostMeter()
    with meter.active():
        report = run_gate(mode, load_thresholds(), meter)
    report = {
        "started_at": started,
        "git_sha": git_short_sha(),
        "o2c_extract": target,
        **report,
    }
    md, js = write_report(report)
    saved = save_gate_result(report)

    print()
    print(format_table(report["checks"]))
    print()
    print(f"gate: {'PASS' if report['passed'] else 'FAIL'}  (wrote {md}, {js})")
    if saved:
        print(f"saved for the migration rule: {saved}")
    if report["llm_fallbacks"]:
        print(f"warning: {report['llm_fallbacks']} calls were answered by a fallback")
    print(
        f"OpenAI cost of this run: ${report['cost_usd']:.4f} "
        f"({report['llm_calls']} LLM calls, sum of x-litellm-response-cost)"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
