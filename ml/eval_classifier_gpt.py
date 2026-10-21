"""Evaluate the LLM document classifier on a dataset.

Usage: uv run python ml/eval_classifier_gpt.py <dataset dir>
  e.g. uv run python ml/eval_classifier_gpt.py ml/data/classifier/holdout

Runs classifier.app.classify with the prompt from load_prompt on every PDF listed in
<dataset>/labels.csv (columns file,doc_type), prints the accuracy, a confusion matrix
and the mistakes, and writes ml/results/classifier-gpt-<dataset>.json.

All LLM calls go through LiteLLM: LITELLM_BASE_URL and LITELLM_MASTER_KEY come from
the environment or .env (run from the repository root).
"""

import csv
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import get_args

from classifier.app import Classification, Settings, classify, load_prompt
from o2c_common.events import Envelope
from o2c_common.pdf import pdf_text
from openai import OpenAI

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DOC_TYPES = list(get_args(Classification.model_fields["doc_type"].annotation))
ERROR = "error"  # predicted label when the classifier call failed

# (pdf text, file name) -> Classification
ClassifyText = Callable[[str, str], Classification]


def read_labels(dataset: Path) -> list[tuple[str, str]]:
    with (dataset / "labels.csv").open(encoding="utf-8", newline="") as f:
        return [(row["file"], row["doc_type"]) for row in csv.DictReader(f)]


def evaluate(
    dataset: Path,
    classify_text: ClassifyText,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    rows = []
    for file, expected in read_labels(dataset):
        predicted, reason = ERROR, None
        try:
            result = classify_text(pdf_text((dataset / file).read_bytes()), file)
            predicted, reason = result.doc_type, result.reason
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            reason = f"{type(exc).__name__}: {exc}"
        row = {
            "file": file,
            "expected": expected,
            "predicted": predicted,
            "reason": reason,
        }
        rows.append(row)
        if progress:
            progress(row)
    return rows


def confusion_matrix(rows: list[dict]) -> dict[str, dict[str, int]]:
    """matrix[expected][predicted] = count; an "error" column only if a call failed."""
    columns = DOC_TYPES + (
        [ERROR] if any(r["predicted"] == ERROR for r in rows) else []
    )
    matrix = {expected: dict.fromkeys(columns, 0) for expected in DOC_TYPES}
    for row in rows:
        matrix[row["expected"]][row["predicted"]] += 1
    return matrix


def summarize(rows: list[dict]) -> dict:
    mistakes = [row for row in rows if row["predicted"] != row["expected"]]
    return {
        "documents": len(rows),
        "accuracy": round(1 - len(mistakes) / len(rows), 4) if rows else 0.0,
        "confusion_matrix": confusion_matrix(rows),
        "mistakes": mistakes,
    }


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


def make_classify_text() -> ClassifyText:
    # Only the LiteLLM fields are used here; the service also requires S3 and database settings.
    settings = Settings(
        s3_access_key="unused", s3_secret_key="unused", events_database_url="unused"
    )
    llm = OpenAI(
        base_url=settings.litellm_base_url, api_key=settings.litellm_master_key
    )
    system_prompt = load_prompt(settings.prompts_dir)

    def classify_text(text: str, file: str) -> Classification:
        # classify() takes the session id for Langfuse from the event.
        key = hashlib.sha256(f"eval-classifier/{file}".encode()).hexdigest()
        event = Envelope.new("docs.received", "eval", key, {"file": file})
        return classify(llm, system_prompt, text, event)

    return classify_text


def main(argv: list[str] | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit(__doc__)
    dataset = Path(args[0])

    rows = evaluate(
        dataset,
        make_classify_text(),
        progress=lambda r: print(
            f"{r['file']}  {r['expected']} -> {r['predicted']}", flush=True
        ),
    )
    summary = summarize(rows)
    print(f"\naccuracy: {summary['accuracy']:.1%} ({summary['documents']} documents)\n")
    print(format_matrix(summary["confusion_matrix"]))
    print(f"\nmistakes ({len(summary['mistakes'])}):")
    for m in summary["mistakes"]:
        print(
            f"  {m['file']}: expected {m['expected']}, got {m['predicted']} - {m['reason']}"
        )

    out = RESULTS_DIR / f"classifier-gpt-{dataset.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"dataset": str(dataset), **summary, "documents": rows},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    main()
