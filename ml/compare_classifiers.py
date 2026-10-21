"""Compare the GPT and the fine-tuned DistilBERT document classifiers on the holdout set.

Usage: uv run python ml/compare_classifiers.py [results dir]

Reads ml/results/classifier-gpt-holdout.json (ml/eval_classifier_gpt.py) and
ml/results/classifier-distilbert-holdout.json (ml/train_classifier.py) and prints
accuracy, mistakes, ms per document and cost per 1000 documents.
GPT cost comes from token usage, priced by LiteLLM (x-litellm-response-cost);
DistilBERT runs locally on CPU, so it has no per-document cost.
"""

import json
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
FILES = {
    "GPT (o2c-extract)": "classifier-gpt-holdout.json",
    "DistilBERT (fine-tuned)": "classifier-distilbert-holdout.json",
}
COLUMNS = ["classifier", "accuracy", "mistakes", "ms/doc", "cost/1000 docs"]


def compare(results_dir: Path = RESULTS_DIR) -> list[dict]:
    rows = []
    for name, file in FILES.items():
        data = json.loads((results_dir / file).read_text(encoding="utf-8"))
        if data.get("classifier") == "distilbert":
            cost = 0.0
        else:  # None if the results predate token-usage recording
            cost = (data.get("usage") or {}).get("cost_per_1000_documents_usd")
        rows.append(
            {
                "classifier": name,
                "documents": data["documents"]
                if isinstance(data["documents"], int)
                else len(data["documents"]),
                "accuracy": data["accuracy"],
                "mistakes": len(data["mistakes"]),
                "ms_per_document": data.get("ms_per_document"),
                "cost_per_1000_documents_usd": cost,
            }
        )
    return rows


def format_table(rows: list[dict]) -> str:
    def cells(row: dict) -> list[str]:
        ms, cost = row["ms_per_document"], row["cost_per_1000_documents_usd"]
        return [
            row["classifier"],
            f"{row['accuracy']:.1%}",
            f"{row['mistakes']}/{row['documents']}",
            "n/a" if ms is None else f"{ms:,.1f}",
            "n/a" if cost is None else f"${cost:.4f}",
        ]

    table = [COLUMNS, *(cells(row) for row in rows)]
    widths = [max(len(line[i]) for line in table) for i in range(len(COLUMNS))]
    lines = [
        "  ".join(
            cell.ljust(w) if i == 0 else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(line, widths, strict=True))
        )
        for line in table
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> str:
    args = sys.argv[1:] if argv is None else argv
    results_dir = Path(args[0]) if args else RESULTS_DIR
    table = format_table(compare(results_dir))
    print(table)
    return table


if __name__ == "__main__":
    main()
