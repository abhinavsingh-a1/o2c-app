"""Shared pieces of the document-classifier evaluations (GPT and fine-tuned DistilBERT).

Labels, text preparation, labels.csv, confusion matrix and the summary format, so both
evaluations write results that ml/compare_classifiers.py can compare. No torch here,
so the unit tests stay fast.
"""

import csv
import json
from pathlib import Path

from o2c_common.pdf import pdf_text

LABELS = ["purchase_order", "invoice", "remittance", "other"]
LABEL2ID = {label: i for i, label in enumerate(LABELS)}
ID2LABEL = dict(enumerate(LABELS))
MAX_CHARS = 1500  # DistilBERT input: the first 1500 characters of the layout-mode text
ERROR = "error"  # predicted label when a classifier call failed


def label_ids(labels: list[str]) -> list[int]:
    """Doc types -> class ids; an unknown doc type raises KeyError."""
    return [LABEL2ID[label] for label in labels]


def label_names(ids: list[int]) -> list[str]:
    return [ID2LABEL[int(i)] for i in ids]


def prepare_text(pdf: bytes) -> str:
    """Model input for one document: the start of its layout-mode text."""
    return pdf_text(pdf)[:MAX_CHARS]


def read_labels(dataset: Path) -> list[tuple[str, str]]:
    """(file relative to the dataset, doc type) from <dataset>/labels.csv."""
    with (dataset / "labels.csv").open(encoding="utf-8", newline="") as f:
        return [(row["file"], row["doc_type"]) for row in csv.DictReader(f)]


def confusion_matrix(rows: list[dict]) -> dict[str, dict[str, int]]:
    """matrix[expected][predicted] = count; an "error" column only if a call failed."""
    columns = LABELS + ([ERROR] if any(r["predicted"] == ERROR for r in rows) else [])
    matrix = {expected: dict.fromkeys(columns, 0) for expected in LABELS}
    for row in rows:
        matrix[row["expected"]][row["predicted"]] += 1
    return matrix


def summarize(rows: list[dict]) -> dict:
    """Accuracy, confusion matrix and mistakes; ms per document if rows have "ms",
    token usage and cost if rows have "usage" (LLM calls)."""
    mistakes = [row for row in rows if row["predicted"] != row["expected"]]
    summary = {
        "documents": len(rows),
        "accuracy": round(1 - len(mistakes) / len(rows), 4) if rows else 0.0,
        "confusion_matrix": confusion_matrix(rows),
        "mistakes": mistakes,
    }
    times = [row["ms"] for row in rows if row.get("ms") is not None]
    if times:
        summary["ms_per_document"] = round(sum(times) / len(times), 1)
    usages = [row["usage"] for row in rows if row.get("usage")]
    if usages:
        costs = [u["cost_usd"] for u in usages if u.get("cost_usd") is not None]
        summary["usage"] = {
            "prompt_tokens": sum(u["prompt_tokens"] for u in usages),
            "completion_tokens": sum(u["completion_tokens"] for u in usages),
            "cost_usd": round(sum(costs), 6) if costs else None,
            "cost_per_1000_documents_usd": (
                round(sum(costs) / len(costs) * 1000, 4) if costs else None
            ),
        }
    return summary


def format_matrix(matrix: dict[str, dict[str, int]]) -> str:
    columns = list(next(iter(matrix.values())))
    width = max(len(c) for c in [*columns, "expected \\ predicted"]) + 2
    lines = [
        "expected \\ predicted".ljust(width) + "".join(c.rjust(width) for c in columns)
    ]
    for expected, counts in matrix.items():
        lines.append(
            expected.ljust(width)
            + "".join(str(counts[c]).rjust(width) for c in columns)
        )
    return "\n".join(lines)


def print_summary(summary: dict) -> None:
    print(f"\naccuracy: {summary['accuracy']:.1%} ({summary['documents']} documents)")
    if "ms_per_document" in summary:
        print(f"average: {summary['ms_per_document']} ms per document")
    print()
    print(format_matrix(summary["confusion_matrix"]))
    print(f"\nmistakes ({len(summary['mistakes'])}):")
    for m in summary["mistakes"]:
        print(
            f"  {m['file']}: expected {m['expected']}, got {m['predicted']} - {m['reason']}"
        )


def write_results(
    path: Path, dataset: Path, summary: dict, rows: list[dict], **extra
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"dataset": str(dataset), **extra, **summary, "documents": rows}
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
