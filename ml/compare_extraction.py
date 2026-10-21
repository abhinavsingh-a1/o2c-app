"""Compare extraction models and apply the migration rule (E11, ADR-008).

Usage: uv run python ml/compare_extraction.py [current alias] [candidate alias ...]
  default: o2c-extract vs o2c-extract-candidate and o2c-extract-candidate-2

Reads the ml/eval_extraction.py results of each model on poc/data, the first 20 POs of
ml/data/train and ml/data/holdout, prints one table (perfect POs, field accuracy, average
latency, cost per PO, fallbacks) and, per candidate, whether it may replace the current
model. The rule (should_migrate): on EVERY dataset
  - perfect POs >= the current model's,
  - field accuracy at most 0.2 points lower,
  - cost per PO at most 50% higher.
"""

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
# (label, results file prefix) - the prefixes eval_extraction.results_path writes
DATASETS = [
    ("poc/data", "data"),
    ("train, first 20", "train-first20"),
    ("holdout", "holdout"),
]
CURRENT = "o2c-extract"
CANDIDATES = ["o2c-extract-candidate", "o2c-extract-candidate-2"]
MAX_ACCURACY_DROP = 0.002  # 0.2 percentage points
MAX_COST_INCREASE = 0.5  # +50%
COLUMNS = ["model", "dataset", "perfect", "field acc.", "latency", "cost/PO", "fallb."]


def load_summaries(model: str, results_dir: Path = RESULTS_DIR) -> dict[str, dict]:
    """dataset label -> summary of the model's results file."""
    summaries = {}
    for label, prefix in DATASETS:
        path = results_dir / f"{prefix}-{model}.json"
        summaries[label] = json.loads(path.read_text(encoding="utf-8"))["summary"]
    return summaries


def compare_dataset(current: dict, candidate: dict) -> list[str]:
    """Reasons why the candidate fails the rule on one dataset; empty if it passes."""
    failures = []
    if candidate["perfect_pos"] < current["perfect_pos"]:
        failures.append(
            f"perfect POs {candidate['perfect_pos']} < {current['perfect_pos']}"
        )
    drop = current["field_accuracy"] - candidate["field_accuracy"]
    if drop > MAX_ACCURACY_DROP + 1e-9:
        failures.append(f"field accuracy {drop * 100:.2f} points lower")
    old, new = current.get("cost_per_po_usd"), candidate.get("cost_per_po_usd")
    if old is None or new is None:
        failures.append("cost per PO not measured")
    elif new > old * (1 + MAX_COST_INCREASE):
        failures.append(f"cost per PO +{(new / old - 1):.0%}")
    return failures


def should_migrate(
    current: dict[str, dict], candidate: dict[str, dict]
) -> tuple[bool, list[str]]:
    """(passes, reasons it fails) over all datasets of the current model."""
    reasons = [
        f"{label}: {failure}"
        for label in current
        for failure in compare_dataset(current[label], candidate[label])
    ]
    return not reasons, reasons


def table_rows(summaries: dict[str, dict[str, dict]]) -> list[list[str]]:
    rows = []
    for model, by_dataset in summaries.items():
        for label, s in by_dataset.items():
            latency, cost = s.get("avg_latency_s"), s.get("cost_per_po_usd")
            rows.append(
                [
                    model,
                    label,
                    f"{s['perfect_pos']}/{s['documents']}",
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
    return decisions


if __name__ == "__main__":
    main()
