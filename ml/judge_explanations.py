"""LLM-as-judge for the agent's exception explanations (ADR-010).

Usage: uv run --group ml python ml/judge_explanations.py [--dataset poc/data|holdout ...]
  (default: both; judge poc/data first after a prompt change, the holdout once at the end)

1. Judge validation: ml/data/judge_calibration.jsonl holds 3 good and 4 deliberately bad
   explanations (invented price, wrong line number, no action, 8 sentences). The judge
   must pass every good one and fail every bad one; otherwise the script stops (exit 1).
2. Runs the agent graph (ml/agent_harness: DryRunErp, InMemorySaver, the real explainer)
   on every price-deviation PO of poc/data and ml/data/holdout (labels.json), starting
   from the ground truth, so the explanation is judged and not the extraction.
3. Judges each explanation with the LiteLLM alias o2c-judge (a stronger model than the
   generator) against the rubric in prompts/judge_explanation.md: faithful, specific,
   actionable, concise, 1-5 each. Pass/fail is decided in code: every criterion >= 4.
   The judge's own verdict is kept for comparison.
4. Attaches the scores to the explain_exception trace in Langfuse (judge_<criterion>,
   judge_pass) and prints: explanations judged, pass rate, average per criterion, judge
   cost (x-litellm-response-cost). Writes ml/results/judge-explanations.json.
LiteLLM is called with LITELLM_KEY_EVAL when it is set (ml/litellm_common.py).
"""

import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from o2c_common.llm import strict_response_format
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
ML_DIR = REPO_ROOT / "ml"
CALIBRATION = ML_DIR / "data" / "judge_calibration.jsonl"
RESULTS = ML_DIR / "results" / "judge-explanations.json"
DATASETS = {
    "poc/data": REPO_ROOT / "poc" / "data",
    "holdout": ML_DIR / "data" / "holdout",
}
JUDGE_MODEL = "o2c-judge"
CRITERIA = ["faithful", "specific", "actionable", "concise"]
PASS_SCORE = 4


class CriterionScore(BaseModel):
    # Strict structured outputs: no extra keys, every field required (ADR-008).
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=1, le=5)
    reason: str


class Judgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    faithful: CriterionScore
    specific: CriterionScore
    actionable: CriterionScore
    concise: CriterionScore
    verdict: Literal["pass", "fail"]
    reason: str


# (facts, explanation) -> (judgement, cost in USD or None)
Judge = Callable[[dict, str], tuple[Judgement, float | None]]


def load_rubric(prompts_dir: Path = REPO_ROOT / "prompts") -> str:
    # The schema goes to the API as a strict response_format, not into the prompt.
    return (prompts_dir / "judge_explanation.md").read_text(encoding="utf-8")


def parse_judgement(text: str) -> Judgement:
    """The judge's JSON answer; ValueError (pydantic ValidationError) if it is invalid."""
    return Judgement.model_validate_json(text)


def scores(judgement: Judgement) -> dict[str, int]:
    return {c: getattr(judgement, c).score for c in CRITERIA}


def passed(judgement: Judgement) -> bool:
    """Pass only if every criterion scores at least PASS_SCORE (decided here, not by the judge)."""
    return all(score >= PASS_SCORE for score in scores(judgement).values())


def make_judge(settings=None) -> Judge:
    from langfuse_common import setting
    from openai import OpenAI

    client = OpenAI(
        base_url=setting("LITELLM_BASE_URL", "http://localhost:4000"),
        api_key=setting("LITELLM_MASTER_KEY"),
    )
    rubric = load_rubric()

    def judge(facts: dict, explanation: str) -> tuple[Judgement, float | None]:
        raw = client.chat.completions.with_raw_response.create(
            model=JUDGE_MODEL,
            response_format=strict_response_format(Judgement),
            messages=[
                {"role": "system", "content": rubric},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"facts": facts, "explanation": explanation}, ensure_ascii=False
                    ),
                },
            ],
            extra_body={
                "metadata": {"trace_name": "judge_explanation", "tags": ["eval"]}
            },
        )
        cost = raw.headers.get("x-litellm-response-cost")
        content = raw.parse().choices[0].message.content
        return parse_judgement(content), float(cost) if cost is not None else None

    return judge


def load_calibration(path: Path = CALIBRATION) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def judge_row(judge: Judge, facts: dict, explanation: str) -> dict:
    judgement, cost = judge(facts, explanation)
    return {
        "scores": scores(judgement),
        "passed": passed(judgement),
        "judge_verdict": judgement.verdict,
        "reason": judgement.reason,
        "criteria_reasons": {c: getattr(judgement, c).reason for c in CRITERIA},
        "cost_usd": cost,
    }


def calibrate(cases: list[dict], judge: Judge) -> list[dict]:
    """Judge every calibration case; "ok" if the outcome matches the expectation."""
    rows = []
    for case in cases:
        row = {"id": case["id"], "expected": case["expected"], "defect": case["defect"]}
        row.update(judge_row(judge, case["facts"], case["explanation"]))
        row["ok"] = row["passed"] == (case["expected"] == "pass")
        rows.append(row)
    return rows


def price_deviation_pos(dataset_dir: Path) -> list[str]:
    labels = json.loads((dataset_dir / "labels.json").read_text(encoding="utf-8"))
    return sorted(po_id for po_id, label in labels.items() if label["price_deviation"])


def results_path(datasets: list[str] | None) -> Path:
    """ml/results/judge-explanations[-<dataset>...].json"""
    if not datasets:
        return RESULTS
    suffix = "-".join(d.replace("/", "_") for d in datasets)
    return RESULTS.with_name(f"judge-explanations-{suffix}.json")


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    return {
        "judged": n,
        "pass_rate": round(sum(r["passed"] for r in rows) / n, 4) if n else 0.0,
        "average": {
            c: round(sum(r["scores"][c] for r in rows) / n, 2) if n else 0.0
            for c in CRITERIA
        },
        "judge_cost_usd": round(sum(costs), 6) if costs else None,
        "verdict_disagreements": sum(
            (r["judge_verdict"] == "pass") != r["passed"] for r in rows
        ),
    }


def format_table(summary: dict) -> str:
    lines = [
        f"explanations judged: {summary['judged']}",
        f"pass rate:           {summary['pass_rate']:.1%}",
        *(f"average {c + ':':<12}{summary['average'][c]:.2f}" for c in CRITERIA),
        "judge cost:          "
        + (
            "n/a"
            if summary["judge_cost_usd"] is None
            else f"${summary['judge_cost_usd']:.4f}"
        ),
    ]
    return "\n".join(lines)


def attach_scores(client, trace_id: str, row: dict) -> None:
    for criterion, value in row["scores"].items():
        client.score(
            trace_id=trace_id,
            name=f"judge_{criterion}",
            value=value,
            comment=row["criteria_reasons"][criterion],
        )
    client.score(
        trace_id=trace_id,
        name="judge_pass",
        value=1.0 if row["passed"] else 0.0,
        comment=row["reason"],
    )


def judge_agent_explanations(
    judge: Judge, langfuse=None, datasets: list[str] | None = None
) -> list[dict]:
    from agent.explain import explanation_facts, make_explainer
    from agent.settings import Settings
    from agent_harness import make_graph, run_agent
    from langfuse_common import trace_id
    from o2c_common.schema import PurchaseOrder

    nonce = uuid.uuid4().hex
    settings = Settings()
    explainer = make_explainer(
        settings, trace_id_for=lambda state: trace_id(nonce, state.idempotency_key)
    )
    graph = make_graph(explain=explainer, settings=settings)
    rows = []
    for label, directory in DATASETS.items():
        if datasets and label not in datasets:
            continue
        for po_id in price_deviation_pos(directory):
            po = PurchaseOrder.model_validate_json(
                (directory / "ground_truth" / f"{po_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            state = run_agent(graph, po, f"judge-{directory.name}-{po_id}")
            row = {
                "dataset": label,
                "po_id": po_id,
                "status": state.status,
                "issues": [i.code.value for i in state.issues],
                "explanation": state.explanation,
                "trace_id": trace_id(nonce, state.idempotency_key),
            }
            row.update(judge_row(judge, explanation_facts(state), state.explanation))
            if langfuse:
                attach_scores(langfuse, row["trace_id"], row)
            print(
                f"{label:9} {po_id}  {'PASS' if row['passed'] else 'FAIL'}  "
                f"{row['scores']}  {row['reason'][:100]}",
                flush=True,
            )
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    from langfuse_common import make_langfuse
    from litellm_common import use_eval_key

    parser = argparse.ArgumentParser(description="Judge the exception explanations.")
    parser.add_argument("--dataset", action="append", choices=list(DATASETS))
    args = parser.parse_args(argv)
    use_eval_key()

    judge = make_judge()
    calibration = calibrate(load_calibration(), judge)
    print("judge validation (ml/data/judge_calibration.jsonl):")
    for row in calibration:
        print(
            f"  {row['id']:22} expected {row['expected']:4} got "
            f"{'pass' if row['passed'] else 'fail'}  {'OK' if row['ok'] else 'WRONG'}  "
            f"{row['scores']}"
        )
    if not all(row["ok"] for row in calibration):
        print("judge validation failed: not judging the agent's explanations")
        return 1
    print("judge validation passed\n")

    langfuse = make_langfuse()
    rows = judge_agent_explanations(judge, langfuse, args.dataset)
    langfuse.flush()
    summary = summarize(rows)
    calibration_cost = summarize(calibration)["judge_cost_usd"]
    print()
    print(format_table(summary))
    print(f"(judge validation cost: ${calibration_cost or 0:.4f})")
    out = results_path(args.dataset)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"summary": summary, "calibration": calibration, "explanations": rows},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ML_DIR))
    sys.exit(main())
