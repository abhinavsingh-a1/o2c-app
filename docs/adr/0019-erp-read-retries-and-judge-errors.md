# ADR-019: Retry ERP reads in the agent; judge errors per PO

Date: 2026-10-28
Status: Accepted

## Context
E14 troubleshooting #29: the eval gate failed once with "judge ran: error" because one
ERP call from the agent timed out. A re-run passed 14/14. A single transient error had:
- failed the whole judge step;
- with it, the promotion to test.

The agent itself relied only on Kafka redelivery for transient ERP errors. That retries
the whole handler, including LLM calls.

## Decision

### ERP client (`agent.erp.ErpClient`)
- **Reads (GET) retry transient errors inside the call:**
  - timeouts, network and protocol errors, and HTTP 502, 503 and 504;
  - 3 attempts with exponential backoff: 0.2 s, then 0.4 s, each plus up to 100% random
    jitter, so replicas do not retry in step.
  - Other statuses (404 = not found, other 4xx, 500) are not retried. A 500 still raises,
    and the consumer's retry and DLQ handle it.
- **Writes (POST `create_order`, `cancel_order`) are never retried here.** A POST that
  timed out may have been applied. The graph's saga handles it: idempotent create on the
  key, `verify_order`, `cancel_order` (ADR-004).
- **Metric:** `o2c_erp_retries_total{call}`, with `call` one of the six read methods. It
  is a new allowed label with a fixed value set (`o2c_common.metrics.ERP_CALLS`, ADR-012).
  Rising retries show a flaky ERP before it causes failures.

### Judge (`ml/judge_explanations.py`)
- If running the agent or judging one PO raises, it is retried **once** under a new
  thread id. The InMemorySaver must not resume a half-finished run.
- A second failure becomes a row with status `error`:
  - it counts as **not passed** in the pass rate;
  - it is excluded from the score averages;
  - it is shown as "(error)" in the gate's detail.
- The other POs are still judged. One bad PO can fail the gate only through the pass-rate
  threshold (>= 90%), not by aborting the step.

## Consequences
- A short ERP blip costs at most about 1.2 s per read instead of a handler retry with
  fresh LLM calls.
- A persistent ERP outage still fails the handler after 3 attempts, and the alerting is
  unchanged (`EventErrorRateHigh`, DLQ).
- An error in the judge is visible per PO in `ml/results/judge-explanations*.json`, not
  hidden by the retry.
