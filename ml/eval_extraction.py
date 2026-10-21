"""Evaluate PO extraction on a dataset.

Usage: uv run python ml/eval_extraction.py <dataset dir> [model alias] [--limit N]
  e.g. uv run python ml/eval_extraction.py ml/data/holdout
       uv run python ml/eval_extraction.py ml/data/holdout o2c-extract-candidate
       uv run python ml/eval_extraction.py ml/data/train --limit 20

Runs extractor.app.make_extractor on every PDF in <dataset>/pos (text via
o2c_common.pdf.pdf_text), scores each result against <dataset>/ground_truth with
o2c_common.scoring.score, prints per-document scores and a JSON summary, and writes
ml/results/<dataset>-<model>.json (<dataset>-first<N>-<model>.json with --limit N, which
evaluates only the first N POs).

All LLM calls go through LiteLLM: LITELLM_BASE_URL and LITELLM_MASTER_KEY come from
the environment or .env (run from the repository root). The model is a LiteLLM alias.
"""

import argparse
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from extractor.app import Extract, Settings, make_extractor
from o2c_common.pdf import pdf_text
from o2c_common.schema import PurchaseOrder
from o2c_common.scoring import score

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_MODEL = "o2c-extract"


def evaluate(
    dataset: Path,
    extract: Extract,
    progress: Callable[[dict], None] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Extract and score every PO (or the first `limit`).
    A failed extraction scores as "invalid output"."""
    rows = []
    for pdf in sorted((dataset / "pos").glob("*.pdf"))[:limit]:
        truth = PurchaseOrder.model_validate_json(
            (dataset / "ground_truth" / f"{pdf.stem}.json").read_text(encoding="utf-8")
        )
        result, exception = None, None
        try:
            result = extract(
                pdf_text(pdf.read_bytes()), f"eval-{dataset.name}-{pdf.stem}"
            )
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            exception = f"{type(exc).__name__}: {exc}"
        row = {
            "document": pdf.stem,
            **score(result, truth),
            "exception": exception,
            "result": result.model_dump(mode="json") if result else None,
        }
        rows.append(row)
        if progress:
            progress(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    correct = sum(row["correct"] for row in rows)
    total = sum(row["total"] for row in rows)
    errors = Counter(error for row in rows for error in row["errors"])
    return {
        "documents": len(rows),
        "perfect_pos": sum(not row["errors"] for row in rows),
        "field_accuracy": round(correct / total, 4) if total else 0.0,
        "top_errors": [{"error": e, "count": n} for e, n in errors.most_common(5)],
    }


def format_row(row: dict) -> str:
    problems = ", ".join(row["errors"]) or "perfect"
    return f"{row['document']}  {row['correct']:>3}/{row['total']:<3}  {problems}"


def results_path(dataset: Path, model: str, limit: int | None = None) -> Path:
    name = dataset.name if limit is None else f"{dataset.name}-first{limit}"
    return RESULTS_DIR / f"{name}-{model.replace('/', '_').replace(':', '_')}.json"


def make_settings() -> Settings:
    # Only the LiteLLM fields are used here; the service also requires S3 and database settings.
    return Settings(
        s3_access_key="unused", s3_secret_key="unused", events_database_url="unused"
    )


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(
        description="Evaluate PO extraction.", usage=__doc__.split("\n\n")[1]
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("model", nargs="?", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, help="evaluate only the first N POs")
    args = parser.parse_args(argv)
    dataset, model, limit = args.dataset, args.model, args.limit

    rows = evaluate(
        dataset,
        make_extractor(make_settings(), model),
        progress=lambda row: print(format_row(row), flush=True),
        limit=limit,
    )
    summary = summarize(rows)
    print(json.dumps(summary, indent=2))

    out = results_path(dataset, model, limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "dataset": str(dataset),
                "model": model,
                "limit": limit,
                "summary": summary,
                "documents": rows,
            },
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
