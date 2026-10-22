"""Evaluate the LLM document classifier on a dataset.

Usage: uv run python ml/eval_classifier_gpt.py <dataset dir>
  e.g. uv run python ml/eval_classifier_gpt.py ml/data/classifier/holdout

Runs classifier.app.classify with the prompt from load_prompt on every PDF listed in
<dataset>/labels.csv (columns file,doc_type), prints the accuracy, a confusion matrix
and the mistakes, and writes ml/results/classifier-gpt-<dataset>.json, including
ms per document and token usage / cost per call (LiteLLM's x-litellm-response-cost).

All LLM calls go through LiteLLM: LITELLM_BASE_URL and LITELLM_MASTER_KEY come from
the environment or .env (run from the repository root).
"""

import hashlib
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from classifier.app import Classification, Settings, classify, load_prompt
from classifier_common import (
    ERROR,
    LABELS,
    confusion_matrix,
    format_matrix,
    print_summary,
    read_labels,
    summarize,
    write_results,
)
from litellm_common import use_eval_key
from o2c_common.events import Envelope
from o2c_common.pdf import pdf_text
from openai import OpenAI

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DOC_TYPES = LABELS

# (pdf text, file name) -> (classification, usage of the LLM call)
ClassifyText = Callable[[str, str], tuple[Classification, dict]]

__all__ = ["confusion_matrix", "format_matrix", "read_labels", "summarize"]


def evaluate(
    dataset: Path,
    classify_text: ClassifyText,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    rows = []
    for file, expected in read_labels(dataset):
        text = pdf_text((dataset / file).read_bytes(), drop_invisible=True)
        predicted, reason, usage = ERROR, None, {}
        start = time.perf_counter()
        try:
            result, usage = classify_text(text, file)
            predicted, reason = result.doc_type, result.reason
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            reason = f"{type(exc).__name__}: {exc}"
        row = {
            "file": file,
            "expected": expected,
            "predicted": predicted,
            "reason": reason,
            "ms": round((time.perf_counter() - start) * 1000, 1),
            "usage": usage,
        }
        rows.append(row)
        if progress:
            progress(row)
    return rows


class UsageRecorder:
    """OpenAI client for classify() that also keeps the token usage and LiteLLM's cost
    of the last chat.completions.create call."""

    def __init__(self, client: OpenAI):
        self._client = client
        self.last: dict = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        raw = self._client.chat.completions.with_raw_response.create(**kwargs)
        response = raw.parse()
        cost = raw.headers.get("x-litellm-response-cost")
        self.last = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "cost_usd": float(cost) if cost is not None else None,
        }
        return response


def make_classify_text() -> ClassifyText:
    # Only the LiteLLM fields are used here; the service also requires S3 and database settings.
    settings = Settings(
        s3_access_key="unused", s3_secret_key="unused", events_database_url="unused"
    )
    llm = UsageRecorder(
        OpenAI(base_url=settings.litellm_base_url, api_key=settings.litellm_master_key)
    )
    system_prompt = load_prompt(settings.prompts_dir)

    def classify_text(text: str, file: str) -> tuple[Classification, dict]:
        # classify() takes the session id for Langfuse from the event.
        key = hashlib.sha256(f"eval-classifier/{file}".encode()).hexdigest()
        event = Envelope.new("docs.received", "eval", key, {"file": file})
        result = classify(llm, system_prompt, text, event)
        return result, dict(llm.last)

    return classify_text


def main(argv: list[str] | None = None) -> Path:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit(__doc__)
    dataset = Path(args[0])
    use_eval_key()

    rows = evaluate(
        dataset,
        make_classify_text(),
        progress=lambda r: print(
            f"{r['file']}  {r['expected']} -> {r['predicted']}", flush=True
        ),
    )
    summary = summarize(rows)
    print_summary(summary)
    out = write_results(
        RESULTS_DIR / f"classifier-gpt-{dataset.name}.json",
        dataset,
        summary,
        rows,
        classifier="gpt",
        model="o2c-extract",
    )
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    main()
