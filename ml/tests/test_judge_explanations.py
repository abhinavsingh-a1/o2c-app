import json

import pytest
from judge_explanations import (
    CRITERIA,
    ERROR,
    Judgement,
    attach_scores,
    calibrate,
    format_table,
    judge_row,
    judge_with_retry,
    load_calibration,
    load_rubric,
    parse_judgement,
    passed,
    price_deviation_pos,
    scores,
    summarize,
)
from o2c_common.llm import strict_response_format
from pydantic import ValidationError


def answer(faithful=5, specific=5, actionable=5, concise=5, verdict="pass") -> str:
    """A recorded judge answer."""
    criteria = {
        "faithful": faithful,
        "specific": specific,
        "actionable": actionable,
        "concise": concise,
    }
    return json.dumps(
        {
            **{c: {"score": s, "reason": f"{c} reason"} for c, s in criteria.items()},
            "verdict": verdict,
            "reason": "overall reason",
        }
    )


def test_parse_judgement():
    judgement = parse_judgement(answer(specific=2, verdict="fail"))

    assert scores(judgement) == {
        "faithful": 5,
        "specific": 2,
        "actionable": 5,
        "concise": 5,
    }
    assert judgement.verdict == "fail"
    assert judgement.specific.reason == "specific reason"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        answer(faithful=6),
        answer(concise=0),
        answer(verdict="maybe"),
        json.dumps({"faithful": {"score": 5, "reason": "x"}}),
    ],
)
def test_parse_judgement_rejects_invalid_answers(text):
    with pytest.raises(ValidationError):
        parse_judgement(text)


def test_pass_needs_every_criterion_at_least_4():
    assert passed(parse_judgement(answer()))
    assert passed(parse_judgement(answer(faithful=4, specific=4)))
    assert not passed(parse_judgement(answer(concise=3)))
    # code decides, even if the judge's own verdict says otherwise
    assert not passed(parse_judgement(answer(actionable=2, verdict="pass")))


def test_rubric_names_all_criteria_without_the_raw_schema():
    rubric = load_rubric()

    assert "{schema}" not in rubric
    assert '"properties"' not in rubric
    assert '"verdict"' in rubric
    for criterion in CRITERIA:
        assert f"- {criterion}:" in rubric
    assert "2 to 4 sentences" in rubric


def test_judgement_is_usable_with_strict_structured_outputs():
    fmt = strict_response_format(Judgement)  # ValueError if not strict-compatible

    assert fmt["json_schema"]["strict"] is True


def test_parse_judgement_rejects_an_answer_wrapped_in_properties():
    with pytest.raises(ValidationError):
        parse_judgement(json.dumps({"properties": json.loads(answer())}))


def test_calibration_set_has_3_good_and_4_bad():
    cases = load_calibration()

    assert sorted(c["expected"] for c in cases) == ["fail"] * 4 + ["pass"] * 3
    assert {c["defect"] for c in cases if c["expected"] == "fail"} == {
        "invented price",
        "wrong line number",
        "no action",
        "8 sentences",
    }
    for case in cases:
        assert case["facts"]["issues"] and case["explanation"]


def fake_judge(answers: dict[str, str]):
    """Judge that returns a recorded answer chosen by the explanation text."""

    def judge(facts, explanation):
        return parse_judgement(answers[explanation]), 0.002

    return judge


def test_calibrate_marks_matching_outcomes():
    cases = [
        {
            "id": "g",
            "expected": "pass",
            "defect": None,
            "facts": {},
            "explanation": "good",
        },
        {
            "id": "b",
            "expected": "fail",
            "defect": "x",
            "facts": {},
            "explanation": "bad",
        },
        {
            "id": "m",
            "expected": "fail",
            "defect": "y",
            "facts": {},
            "explanation": "missed",
        },
    ]
    judge = fake_judge(
        {
            "good": answer(),
            "bad": answer(faithful=1, verdict="fail"),
            "missed": answer(),
        }
    )

    rows = calibrate(cases, judge)

    assert [r["ok"] for r in rows] == [True, True, False]
    assert rows[1]["scores"]["faithful"] == 1


def test_judge_row():
    row = judge_row(fake_judge({"e": answer(specific=3, verdict="fail")}), {}, "e")

    assert row["passed"] is False
    assert row["judge_verdict"] == "fail"
    assert row["criteria_reasons"]["specific"] == "specific reason"
    assert row["cost_usd"] == 0.002


def test_summarize_and_table():
    rows = [
        judge_row(fake_judge({"e": answer()}), {}, "e"),
        judge_row(fake_judge({"e": answer(specific=2, verdict="pass")}), {}, "e"),
    ]

    summary = summarize(rows)

    assert summary["judged"] == 2
    assert summary["pass_rate"] == 0.5
    assert summary["average"]["specific"] == 3.5
    assert summary["judge_cost_usd"] == 0.004
    assert summary["verdict_disagreements"] == 1
    table = format_table(summary)
    assert "pass rate:           50.0%" in table
    assert "$0.0040" in table


def test_summarize_empty():
    summary = summarize([])
    assert summary["judged"] == 0 and summary["judge_cost_usd"] is None


def test_price_deviation_pos(tmp_path):
    (tmp_path / "labels.json").write_text(
        json.dumps(
            {
                "PO-0002": {"price_deviation": True, "line_no": 5},
                "PO-0001": {"price_deviation": False, "line_no": None},
                "PO-0003": {"price_deviation": True, "line_no": 1},
            }
        )
    )

    assert price_deviation_pos(tmp_path) == ["PO-0002", "PO-0003"]


def test_attach_scores():
    calls = []
    client = type("C", (), {"score": lambda self, **kw: calls.append(kw)})()
    row = judge_row(fake_judge({"e": answer(concise=2, verdict="fail")}), {}, "e")

    attach_scores(client, "t" * 32, row)

    assert {c["name"]: c["value"] for c in calls} == {
        "judge_faithful": 5,
        "judge_specific": 5,
        "judge_actionable": 5,
        "judge_concise": 2,
        "judge_pass": 0.0,
    }
    assert {c["trace_id"] for c in calls} == {"t" * 32}


def test_results_path_per_dataset():
    from judge_explanations import RESULTS, results_path

    assert results_path(None) == RESULTS
    assert results_path(["poc/data"]).name == "judge-explanations-poc_data.json"
    assert results_path(["holdout"]).name == "judge-explanations-holdout.json"


def flaky_attempt(failures: int):
    """attempt(name) that raises `failures` times, then returns a judged row."""
    names: list[str] = []

    def attempt(name: str) -> dict:
        names.append(name)
        if len(names) <= failures:
            raise TimeoutError(f"LiteLLM timed out ({len(names)})")
        row = {"dataset": "holdout", "po_id": "PO-1", "status": "awaiting_review"}
        row.update(judge_row(fake_judge({"e": answer()}), {}, "e"))
        return row

    return attempt, names


def test_judge_with_retry_succeeds_first_time():
    attempt, names = flaky_attempt(0)

    row = judge_with_retry(attempt, "judge-holdout-PO-1", "holdout", "PO-1")

    assert row["passed"] is True
    assert names == ["judge-holdout-PO-1"]


def test_judge_with_retry_retries_once_with_a_fresh_name():
    attempt, names = flaky_attempt(1)

    row = judge_with_retry(attempt, "judge-holdout-PO-1", "holdout", "PO-1")

    assert row["passed"] is True
    assert names == ["judge-holdout-PO-1", "judge-holdout-PO-1-retry"]


def test_judge_with_retry_records_an_error_after_two_failures():
    attempt, names = flaky_attempt(2)

    row = judge_with_retry(attempt, "judge-holdout-PO-1", "holdout", "PO-1")

    assert len(names) == 2
    assert row["status"] == ERROR
    assert row["passed"] is False
    assert row["scores"] is None
    assert row["error"] == "TimeoutError: LiteLLM timed out (2)"
    assert (row["dataset"], row["po_id"]) == ("holdout", "PO-1")


def test_summarize_counts_errors_as_not_passed():
    ok, _ = flaky_attempt(0)
    broken, _ = flaky_attempt(2)
    rows = [
        judge_with_retry(ok, "a", "holdout", "PO-1"),
        judge_with_retry(broken, "b", "holdout", "PO-2"),
    ]

    summary = summarize(rows)

    assert summary["judged"] == 2
    assert summary["errors"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["average"]["faithful"] == 5.0  # averages over judged rows only
    assert summary["verdict_disagreements"] == 0
    assert "errors:              1" in format_table(summary)


def test_summarize_only_errors():
    broken, _ = flaky_attempt(2)

    summary = summarize([judge_with_retry(broken, "b", "holdout", "PO-2")])

    assert summary["pass_rate"] == 0.0
    assert summary["average"] == {c: 0.0 for c in CRITERIA}
