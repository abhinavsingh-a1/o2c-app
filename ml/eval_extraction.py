"""Evaluate PO extraction on a dataset.

Usage: uv run python ml/eval_extraction.py <dataset dir> [model alias] [--limit N]
  e.g. uv run python ml/eval_extraction.py ml/data/holdout
       uv run --group ml python ml/eval_extraction.py poc/data --langfuse
       uv run python ml/eval_extraction.py ml/data/holdout o2c-extract-candidate
       uv run python ml/eval_extraction.py ml/data/train --limit 20

Runs extractor.app.make_extractor on every PDF in <dataset>/pos (text via
o2c_common.pdf.pdf_text), scores each result against <dataset>/ground_truth with
o2c_common.scoring.score, prints per-document scores and a JSON summary (perfect POs,
field accuracy, average latency, cost per PO from LiteLLM's x-litellm-response-cost
header, and how many calls LiteLLM answered from a fallback model), and writes
ml/results/<dataset>-<model>.json (<dataset>-first<N>-<model>.json with --limit N, which
evaluates only the first N POs).

--langfuse (poc/data = dataset "golden", ml/data/holdout = "holdout", uploaded by
ml/langfuse_datasets.py): each extraction gets its own Langfuse trace id, the trace is
linked to its dataset item as dataset run "<model>-<git short sha>", and the field
accuracy (and perfect 1/0) are attached to the trace as scores, so runs can be compared
in the Langfuse UI.

All LLM calls go through LiteLLM: LITELLM_BASE_URL and LITELLM_MASTER_KEY come from
the environment or .env (run from the repository root). The model is a LiteLLM alias.
"""

import argparse
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
from extractor.app import Extract, Settings, make_extractor
from o2c_common.pdf import pdf_text
from o2c_common.schema import PurchaseOrder
from o2c_common.scoring import score

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_MODEL = "o2c-extract"
COST_HEADER = "x-litellm-response-cost"
FALLBACK_HEADER = "x-litellm-attempted-fallbacks"


class ResponseRecorder:
    """httpx response hook: collects LiteLLM's cost and fallback headers of the calls
    made since the last take()."""

    def __init__(self):
        self.costs: list[float] = []
        self.fallbacks = 0

    def __call__(self, response: httpx.Response) -> None:
        cost = response.headers.get(COST_HEADER)
        if cost is not None:
            self.costs.append(float(cost))
        if int(response.headers.get(FALLBACK_HEADER) or 0) > 0:
            self.fallbacks += 1

    def take(self) -> dict:
        """{"cost_usd": sum or None, "fallbacks": n} of the recorded calls; then reset."""
        taken = {
            "cost_usd": round(sum(self.costs), 8) if self.costs else None,
            "fallbacks": self.fallbacks,
        }
        self.costs, self.fallbacks = [], 0
        return taken

    def client(self) -> httpx.Client:
        return httpx.Client(event_hooks={"response": [self]})


class LangfuseRun:
    """A Langfuse dataset run: one trace id per PO, then link and score each result."""

    def __init__(self, client, dataset_name: str, run_name: str, nonce: str = ""):
        self.client = client
        self.dataset_name = dataset_name
        self.run_name = run_name
        self.nonce = nonce or uuid.uuid4().hex  # a re-run gets new traces
        self.items = {item.id: item for item in client.get_dataset(dataset_name).items}

    def trace_id(self, po_id: str) -> str:
        from langfuse_common import trace_id

        return trace_id(self.run_name, self.nonce, po_id)

    def record(self, po_id: str, row: dict) -> None:
        from langfuse_datasets import item_id

        trace = self.trace_id(po_id)
        self.items[item_id(self.dataset_name, po_id)].link(
            None,
            self.run_name,
            trace_id=trace,
            run_metadata={"dataset": self.dataset_name},
        )
        accuracy = row["correct"] / row["total"] if row["total"] else 0.0
        errors = ", ".join(row["errors"]) or None
        self.client.score(
            trace_id=trace,
            name="field_accuracy",
            value=round(accuracy, 4),
            comment=errors,
        )
        self.client.score(
            trace_id=trace, name="perfect", value=0.0 if row["errors"] else 1.0
        )


def langfuse_dataset_name(dataset: Path) -> str:
    """The Langfuse dataset of a dataset directory; SystemExit for any other directory."""
    from langfuse_datasets import DATASETS

    for name, directory in DATASETS.items():
        if dataset.resolve() == directory.resolve():
            return name
    raise SystemExit(
        f"--langfuse needs one of {', '.join(str(d) for d in DATASETS.values())}"
    )


def run_name(model: str, sha: str) -> str:
    return f"{model}-{sha}"


def evaluate(
    dataset: Path,
    extract: Extract,
    progress: Callable[[dict], None] | None = None,
    limit: int | None = None,
    recorder: ResponseRecorder | None = None,
    langfuse_run: LangfuseRun | None = None,
) -> list[dict]:
    """Extract and score every PO (or the first `limit`), with latency per PO and, given
    the recorder of the extractor's HTTP client, cost and fallbacks per PO.
    A failed extraction scores as "invalid output"."""
    rows = []
    for pdf in sorted((dataset / "pos").glob("*.pdf"))[:limit]:
        truth = PurchaseOrder.model_validate_json(
            (dataset / "ground_truth" / f"{pdf.stem}.json").read_text(encoding="utf-8")
        )
        text = pdf_text(pdf.read_bytes(), drop_invisible=True)  # as the extractor
        result, exception = None, None
        if recorder:
            recorder.take()  # drop anything recorded outside this document
        start = time.perf_counter()
        session = f"eval-{dataset.name}-{pdf.stem}"
        try:
            if langfuse_run:
                result = extract(
                    text, session, trace_id=langfuse_run.trace_id(pdf.stem)
                )
            else:
                result = extract(text, session)
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            exception = f"{type(exc).__name__}: {exc}"
        latency = round(time.perf_counter() - start, 3)
        calls = recorder.take() if recorder else {"cost_usd": None, "fallbacks": 0}
        row = {
            "document": pdf.stem,
            **score(result, truth),
            "exception": exception,
            "latency_s": latency,
            **calls,
            "result": result.model_dump(mode="json") if result else None,
        }
        rows.append(row)
        if langfuse_run:
            langfuse_run.record(pdf.stem, row)
        if progress:
            progress(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    correct = sum(row["correct"] for row in rows)
    total = sum(row["total"] for row in rows)
    errors = Counter(error for row in rows for error in row["errors"])
    latencies = [row["latency_s"] for row in rows if row.get("latency_s") is not None]
    costs = [row["cost_usd"] for row in rows if row.get("cost_usd") is not None]
    return {
        "documents": len(rows),
        "perfect_pos": sum(not row["errors"] for row in rows),
        "field_accuracy": round(correct / total, 4) if total else 0.0,
        "avg_latency_s": (
            round(sum(latencies) / len(latencies), 2) if latencies else None
        ),
        "cost_per_po_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "fallbacks": sum(row.get("fallbacks", 0) for row in rows),
        "top_errors": [{"error": e, "count": n} for e, n in errors.most_common(5)],
    }


def format_row(row: dict) -> str:
    problems = ", ".join(row["errors"]) or "perfect"
    latency = f"  {row['latency_s']:.1f}s" if row.get("latency_s") is not None else ""
    return (
        f"{row['document']}  {row['correct']:>3}/{row['total']:<3}{latency}  {problems}"
    )


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
    parser.add_argument(
        "--langfuse", action="store_true", help="record a Langfuse dataset run"
    )
    args = parser.parse_args(argv)
    dataset, model, limit = args.dataset, args.model, args.limit

    langfuse_run = None
    if args.langfuse:
        from langfuse_common import git_short_sha, make_langfuse

        name = run_name(model, git_short_sha())
        langfuse_run = LangfuseRun(
            make_langfuse(), langfuse_dataset_name(dataset), name
        )
        print(f"Langfuse dataset run {name} on {langfuse_run.dataset_name}")

    recorder = ResponseRecorder()
    rows = evaluate(
        dataset,
        make_extractor(make_settings(), model, http_client=recorder.client()),
        progress=lambda row: print(format_row(row), flush=True),
        limit=limit,
        recorder=recorder,
        langfuse_run=langfuse_run,
    )
    if langfuse_run:
        langfuse_run.client.flush()
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
