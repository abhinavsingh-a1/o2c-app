"""Compare extraction models and apply the noise-aware migration rule (E11, ADR-008).

Usage: uv run python ml/compare_extraction.py [current alias] [candidate alias ...]
  default: o2c-extract vs o2c-extract-candidate and o2c-extract-candidate-low

GPT extraction is not deterministic (ADR-002): the same model on the same POs gives a
different result from run to run. So each model is run RUNS times on poc/data and on the
first 20 POs of ml/data/train, and compared by its averages; the holdout is run once, as
the final check only:

  uv run python ml/eval_extraction.py poc/data <alias> --run 1   (2, 3)
  uv run python ml/eval_extraction.py ml/data/train <alias> --limit 20 --run 1   (2, 3)
  uv run python ml/eval_extraction.py ml/data/holdout <alias>

Prints one table (perfect POs as mean and range, field accuracy, latency, cost per PO and
fallbacks, all averaged over the runs) and, per candidate, whether it may replace the
current model (should_migrate). A candidate passes if
  - poc/data and train: mean perfect POs >= the current model's mean - 1 and mean field
    accuracy at most 0.2 points lower;
  - holdout: perfect POs >= the current model's - 1;
  - every dataset: cost per PO at most 50% higher and mean latency at most 2.5x.
Of the candidates that pass, the best has the most perfect POs over all datasets (then
the lower cost).
"""

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RUNS = 3
# (label, results file prefix, repeated) - the prefixes eval_extraction.results_path writes
DATASETS = [
    ("poc/data", "data", True),
    ("train, first 20", "train-first20", True),
    ("holdout", "holdout", False),
]
CURRENT = "o2c-extract"
CANDIDATES = ["o2c-extract-candidate", "o2c-extract-candidate-low"]
MAX_PERFECT_DROP = 1  # perfect POs (mean over the runs)
MAX_ACCURACY_DROP = 0.002  # 0.2 percentage points
MAX_COST_INCREASE = 0.5  # +50%
MAX_LATENCY_FACTOR = 2.5
COLUMNS = [
    "model",
    "dataset",
    "runs",
    "perfect (mean)",
    "range",
    "field acc.",
    "latency",
    "cost/PO",
    "fallb.",
]

EPS = 1e-9


def _mean(values: list[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def mean_summary(runs: list[dict]) -> dict:
    """The average of several eval_extraction summaries of the same model and dataset."""
    perfect = [s["perfect_pos"] for s in runs]
    return {
        "runs": len(runs),
        "documents": runs[0]["documents"],
        "perfect_pos": sum(perfect) / len(perfect),
        "perfect_min": min(perfect),
        "perfect_max": max(perfect),
        "field_accuracy": _mean([s["field_accuracy"] for s in runs]),
        "avg_latency_s": _mean([s.get("avg_latency_s") for s in runs]),
        "cost_per_po_usd": _mean([s.get("cost_per_po_usd") for s in runs]),
        "fallbacks": sum(s.get("fallbacks", 0) for s in runs),
    }


def result_files(model: str, prefix: str, repeated: bool, results_dir: Path):
    if not repeated:
        return [results_dir / f"{prefix}-{model}.json"]
    return [results_dir / f"{prefix}-{model}-run{i}.json" for i in range(1, RUNS + 1)]


def load_summaries(
    model: str, results_dir: Path = RESULTS_DIR
) -> dict[str, dict | None]:
    """dataset label -> mean summary over the model's runs. Every run of poc/data and
    train must exist (FileNotFoundError); a missing holdout result is None (not measured)."""
    summaries = {}
    for label, prefix, repeated in DATASETS:
        files = result_files(model, prefix, repeated, results_dir)
        if not repeated and not files[0].exists():
            summaries[label] = None
            continue
        runs = [json.loads(f.read_text(encoding="utf-8"))["summary"] for f in files]
        summaries[label] = mean_summary(runs)
    return summaries


def compare_dataset(current: dict, candidate: dict, repeated: bool) -> list[str]:
    """Reasons why the candidate fails the rule on one dataset; empty if it passes."""
    failures = []
    if candidate["perfect_pos"] < current["perfect_pos"] - MAX_PERFECT_DROP - EPS:
        failures.append(
            f"perfect POs {candidate['perfect_pos']:.2f} < "
            f"{current['perfect_pos']:.2f} - {MAX_PERFECT_DROP}"
        )
    if repeated:
        drop = current["field_accuracy"] - candidate["field_accuracy"]
        if drop > MAX_ACCURACY_DROP + EPS:
            failures.append(f"field accuracy {drop * 100:.2f} points lower")
    old, new = current.get("cost_per_po_usd"), candidate.get("cost_per_po_usd")
    if old is None or new is None:
        failures.append("cost per PO not measured")
    elif new > old * (1 + MAX_COST_INCREASE) + EPS:
        failures.append(f"cost per PO +{(new / old - 1):.0%}")
    old, new = current.get("avg_latency_s"), candidate.get("avg_latency_s")
    if old is None or new is None:
        failures.append("latency not measured")
    elif new > old * MAX_LATENCY_FACTOR + EPS:
        failures.append(f"latency {new / old:.1f}x")
    return failures


def should_migrate(
    current: dict[str, dict | None], candidate: dict[str, dict | None]
) -> tuple[bool, list[str]]:
    """(passes, reasons it fails) over all datasets."""
    reasons = []
    for label, _, repeated in DATASETS:
        if current.get(label) is None or candidate.get(label) is None:
            reasons.append(f"{label}: not measured")
            continue
        reasons += [
            f"{label}: {failure}"
            for failure in compare_dataset(current[label], candidate[label], repeated)
        ]
    return not reasons, reasons


def best_candidate(
    decisions: dict[str, bool], summaries: dict[str, dict[str, dict | None]]
) -> str | None:
    """The passing candidate with the most perfect POs over all datasets, then the
    lowest mean cost per PO; None if no candidate passes."""
    passing = [model for model, ok in decisions.items() if ok]
    if not passing:
        return None

    def key(model: str) -> tuple[float, float]:
        by_dataset = [s for s in summaries[model].values() if s]
        perfect = sum(s["perfect_pos"] for s in by_dataset)
        cost = _mean([s["cost_per_po_usd"] for s in by_dataset]) or 0.0
        return (-perfect, cost)

    return min(passing, key=key)


def table_rows(summaries: dict[str, dict[str, dict | None]]) -> list[list[str]]:
    rows = []
    for model, by_dataset in summaries.items():
        for label, s in by_dataset.items():
            if s is None:
                rows.append([model, label, "0", *["n/a"] * (len(COLUMNS) - 3)])
                continue
            latency, cost = s.get("avg_latency_s"), s.get("cost_per_po_usd")
            rows.append(
                [
                    model,
                    label,
                    str(s["runs"]),
                    f"{s['perfect_pos']:.2f}/{s['documents']}",
                    f"{s['perfect_min']}-{s['perfect_max']}",
                    f"{s['field_accuracy']:.2%}",
                    "n/a" if latency is None else f"{latency:.1f} s",
                    "n/a" if cost is None else f"${cost:.5f}",
                    str(s.get("fallbacks", 0)),
                ]
            )
    return rows


def format_table(rows: list[list[str]]) -> str:
    table = [COLUMNS, *rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict[str, bool]:
    args = sys.argv[1:] if argv is None else argv
    current, candidates = (args[0], args[1:]) if args else (CURRENT, CANDIDATES)
    summaries = {model: load_summaries(model) for model in [current, *candidates]}
    print(format_table(table_rows(summaries)))
    print()
    decisions = {}
    for candidate in candidates:
        ok, reasons = should_migrate(summaries[current], summaries[candidate])
        decisions[candidate] = ok
        verdict = "PASSES" if ok else "fails: " + "; ".join(reasons)
        print(f"{candidate} {verdict}")
    best = best_candidate(decisions, summaries)
    print(f"best passing candidate: {best or 'none'}")
    return decisions


if __name__ == "__main__":
    main()
