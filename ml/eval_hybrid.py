"""Measure the hybrid document classifier (risk-based routing, ADR-009) on a dataset.

Usage: uv run --group ml python ml/eval_hybrid.py [dataset dir] [non-PO threshold]
  default: ml/data/classifier/holdout and the service default non-PO threshold

Uses the service's own code path: classifier.hybrid.classify_hybrid with
models:/o2c-doc-classifier@champion (MLFLOW_TRACKING_URI, default http://localhost:5000)
and classifier.app.classify through LiteLLM for the documents it routes to GPT.
Prints accuracy, false purchase_order labels (must be 0: a false PO could create a wrong
order), routing reasons, the share of documents sent to GPT, ms per document (DistilBERT plus,
where used, the GPT call) and cost per 1000 documents (LiteLLM's x-litellm-response-cost;
DistilBERT costs nothing per document), and writes ml/results/classifier-hybrid-<dataset>.json.
"""

import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from classifier.app import DEFAULT_NON_PO_THRESHOLD
from classifier.hybrid import (
    MODEL_UNAVAILABLE,
    PURCHASE_ORDER,
    HybridResult,
    classify_hybrid,
    load_distilbert,
)
from classifier_common import (
    ERROR,
    print_summary,
    read_labels,
    summarize,
    write_results,
)
from eval_classifier_gpt import make_classify_text
from o2c_common.pdf import pdf_text

RESULTS_DIR = Path(__file__).resolve().parent / "results"
HOLDOUT = Path(__file__).resolve().parent / "data" / "classifier" / "holdout"

# (pdf text, file name) -> (hybrid result, usage of the GPT call or {})
ClassifyHybrid = Callable[[str, str], tuple[HybridResult, dict]]


def evaluate(
    dataset: Path,
    classify: ClassifyHybrid,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    rows = []
    for file, expected in read_labels(dataset):
        text = pdf_text((dataset / file).read_bytes())
        start = time.perf_counter()
        try:
            result, usage = classify(text, file)
            predicted, reason = result.doc_type, result.reason
            by, confidence = result.classified_by, result.confidence
            routing = result.routing_reason
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            predicted, reason, by, confidence, routing, usage = (
                ERROR,
                f"{type(exc).__name__}: {exc}",
                "gpt",
                None,
                MODEL_UNAVAILABLE,
                {},
            )
        row = {
            "file": file,
            "expected": expected,
            "predicted": predicted,
            "reason": reason,
            "classified_by": by,
            "confidence": confidence,
            "routing_reason": routing,
            "ms": round((time.perf_counter() - start) * 1000, 1),
            "usage": usage,
        }
        rows.append(row)
        if progress:
            progress(row)
    return rows


def summarize_hybrid(rows: list[dict]) -> dict:
    """classifier_common.summarize plus false purchase orders, routing reasons, the GPT
    share and the cost per 1000 documents over ALL documents (DistilBERT costs 0)."""
    summary = summarize(rows)
    summary["false_purchase_orders"] = sum(
        r["predicted"] == PURCHASE_ORDER and r["expected"] != PURCHASE_ORDER
        for r in rows
    )
    summary["routing"] = dict(Counter(r["routing_reason"] for r in rows))
    to_gpt = sum(row["classified_by"] == "gpt" for row in rows)
    costs = [(row.get("usage") or {}).get("cost_usd") or 0.0 for row in rows]
    summary["sent_to_gpt"] = to_gpt
    summary["gpt_share"] = round(to_gpt / len(rows), 4) if rows else 0.0
    summary["cost_per_1000_documents_usd"] = (
        round(sum(costs) / len(rows) * 1000, 4) if rows else 0.0
    )
    return summary


def make_classify_hybrid(non_po_threshold: float) -> ClassifyHybrid:
    local = load_distilbert(
        os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    )
    if local is None:
        raise SystemExit("DistilBERT model unavailable")
    classify_text = make_classify_text()

    def classify(text: str, file: str) -> tuple[HybridResult, dict]:
        usage = {}

        def gpt(t: str) -> tuple[str, str]:
            result, call_usage = classify_text(t, file)
            usage.update(call_usage)
            return result.doc_type, result.reason

        return classify_hybrid(text, local, non_po_threshold, gpt), usage

    return classify


def main(argv: list[str] | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    dataset = Path(args[0]) if args else HOLDOUT
    threshold = float(args[1]) if len(args) > 1 else DEFAULT_NON_PO_THRESHOLD

    rows = evaluate(
        dataset,
        make_classify_hybrid(threshold),
        progress=lambda r: print(
            f"{r['file']}  {r['expected']} -> {r['predicted']}  "
            f"({r['classified_by']}, {r['routing_reason']}, "
            f"p={r['confidence'] or 0:.3f}, {r['ms']:.0f} ms)",
            flush=True,
        ),
    )
    summary = summarize_hybrid(rows)
    print_summary(summary)
    print(f"false purchase_order labels: {summary['false_purchase_orders']}")
    print(f"routing: {summary['routing']}")
    print(
        f"non-PO threshold {threshold}: {summary['sent_to_gpt']}/{summary['documents']} "
        f"({summary['gpt_share']:.1%}) sent to GPT, "
        f"${summary['cost_per_1000_documents_usd']:.4f} per 1000 documents"
    )
    out = write_results(
        RESULTS_DIR / f"classifier-hybrid-{dataset.name}.json",
        dataset,
        summary,
        rows,
        classifier="hybrid",
        model="o2c-doc-classifier@champion + o2c-extract",
        routing="risk-based (non-PO only)",
        non_po_threshold=threshold,
    )
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    main()
