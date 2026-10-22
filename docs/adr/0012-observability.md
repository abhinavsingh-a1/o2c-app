# ADR-012: Observability with Prometheus and Grafana

Date: 2026-10-22
Status: Accepted

## Context
Langfuse shows single LLM calls, `make lag` and `make dlq-list` show Kafka on demand,
and the ERP mock and the review UI show single orders. Nothing answers "is the pipeline
healthy right now, and is it doing its job?": throughput, the share of orders posted
without a person, where exceptions come from, stuck consumers, the DLQ, latency and LLM
cost. Nobody is told when one of these goes wrong.

## Decision

### Metrics (`prometheus-client`, all defined in `o2c_common.metrics`)
One module defines every `o2c_*` metric, so names and labels are the same in every
service and one test checks the label rule for all of them.

Technical:

| Metric | Labels | Where |
|---|---|---|
| `o2c_http_requests_total`, `o2c_http_request_duration_seconds` | method, route (template), status | middleware: intake (Flask), extractor, agent, erp-mock (FastAPI) |
| `o2c_events_consumed_total` | group | `EventConsumer.process`, so every consumer gets it |
| `o2c_events_failed_total` (handler attempts that raised) | group | same |
| `o2c_events_dlq_total` | group, reason (invalid_envelope, permanent_error, retries_exhausted) | same |
| `o2c_events_duplicates_total` | group | same (idempotency skips) |
| `o2c_event_handler_seconds` (per attempt) | group | same |

Business:

| Metric | Labels | Where |
|---|---|---|
| `o2c_documents_received_total` | source | intake, after the event is stored in Kafka |
| `o2c_documents_classified_total` | doc_type, classified_by | classifier |
| `o2c_extraction_seconds` | - | extractor (each LLM extraction, also `POST /extract`) |
| `o2c_orders_total` | outcome (posted, awaiting_review, rejected, failed), decided_by (agent, reviewer) | agent |
| `o2c_issues_total` | code (IssueCode) | agent |
| `o2c_reviews_total` | action | agent, `POST /reviews/{key}` |
| `o2c_llm_cost_usd_total`, `o2c_llm_calls_total` | service | httpx response hook on LiteLLM's `x-litellm-response-cost` (classifier, extractor, agent incl. embeddings) |

- **Orders are counted once per decision maker.** `decided_by="agent"` is counted once
  per order, when the agent first decides it. `decided_by="reviewer"` is counted at each
  review decision.
  - The touchless rate is `posted/agent ÷ all/agent`. It is not inflated by orders that
    a reviewer posts later.
  - A redelivered `orders.extracted` event does not count the order again
    (`checkpoint.is_decided`).
- **Exposition:**
  - FastAPI services serve `GET /metrics` (`instrument_fastapi`).
  - The classifier serves it on its health server (port 8301).
  - Intake runs gunicorn with 2 workers and uses prometheus_client's multiprocess mode:
    `PROMETHEUS_MULTIPROC_DIR`, plus `intake/gunicorn_conf.py`, which empties the
    directory at start and marks dead workers.
- **Series start at zero.** Label sets known in advance are created at 0 at startup:
  outcomes, actions, doc types, issue codes, each consumer group's series, and each
  service's LLM cost. Without this, `increase()` misses the first increment of a series
  that appears with it, which would undercount the touchless and cost alerts.
- **Every service exports every `o2c_*` metric**, with zeros for the ones it does not
  use (for example, `o2c_extraction_seconds` at 0 on the agent). This is the price of one
  shared module; queries `sum` over jobs, so totals are unaffected.
- **LiteLLM** also exports its own metrics (`callbacks: ["prometheus"]`): spend, tokens
  and latency per model and key.
  - Its `/metrics/` endpoint is unauthenticated
    (`require_auth_for_metrics_endpoint: false`). Prometheus would otherwise need the
    master key.
  - Key labels are aliases and hashed tokens, not keys. Port 4000 is only published
    locally. In the cluster (E14) only Prometheus may reach it (NetworkPolicy).
- **kafka-exporter** provides consumer lag per group and the DLQ topic's offsets.

### Label cardinality rule
A label value must come from a small set known in advance:
- route templates (`/reviews/{idempotency_key}`, never the path; `unmatched` for 404s)
- HTTP methods (others folded into `other`)
- status codes, consumer groups, doc types, statuses, issue codes, actions, services

Never use an idempotency key, event id, PO number, customer, file name, sender, user or
raw path as a label: each would create one series per document or user, and Prometheus
memory grows with the number of series. Per-order detail belongs in Langfuse, the review
store and the logs.

Tests enforce the rule:
- `test_metrics.py` checks that every defined metric's labels are within
  `ALLOWED_LABELS`, and that a request with a key in the path does not leak the key into
  any sample.
- `observability/tests` checks that rules and panels only aggregate by allowed labels.

### Alerts (`observability/prometheus/alerts.yml`, promtool-tested: `make prom-test`)

| Alert | Condition | Why |
|---|---|---|
| ServiceDown (critical) | `up == 0` for 2m | A stopped consumer does not fail loudly: documents just wait. 2 minutes rides out a restart or redeploy. |
| DeadLetterQueueReceivedMessages | DLQ offsets increased in the last 10m | Every DLQ message is a document that will not be processed without a person (`make dlq-list` / `dlq-replay`). |
| ConsumerLagHigh | lag per group > 50 for 5m | 50 is far above the demo volume. Sustained lag means a stuck or too slow consumer, not a burst. |
| TouchlessRateLow | posted ÷ decided by the agent < 50% over 1h, with >= 5 orders | The business KPI. A drop means a prompt, model or master-data change sends orders to review. The minimum of 5 avoids alerts on 1 of 2 orders. `or vector(0)` makes "nothing posted" 0%, not "no data". |
| LlmCostHigh | service LLM cost > $0.50 in 1h | About 50x the expected local spend; catches loops early, before the LiteLLM key budgets (ADR-011) reject calls. |
| HttpErrorRateHigh | 5xx ÷ requests > 5% per service for 5m | Client errors (401, 404, 422) are not failures of the service. |
| EventErrorRateHigh | failed handler attempts ÷ consumed > 5% per group for 5m | Catches transient failures (LiteLLM or ERP down) that retries still hide, before messages reach the DLQ. |

- **Thresholds** are for the local stack and the demo volume. They go into the Helm
  values per environment with E14.
- **No Alertmanager yet.** Alerts are visible in Prometheus, in the dashboard and with
  `make prom-alerts`. Routing to ServiceNow or chat comes with the cluster.

### Grafana (provisioning as code, `observability/grafana/`)
- **Provisioning:** the Prometheus datasource has the fixed uid `prometheus`. The
  dashboard "O2C Operations" is generated by `build_dashboard.py`; a test fails if the
  JSON is stale. The UI cannot save changes.
- **Panels:**
  - documents and orders per minute
  - touchless rate
  - exceptions by issue code
  - reviews by action
  - consumer lag per group
  - DLQ
  - event errors
  - p95 HTTP latency per service
  - p95 handler and extraction time
  - LLM cost per service, plus LiteLLM's total spend including evaluations
  - firing alerts
- **Access:** Grafana runs at http://localhost:3001 (3000 is Langfuse), user admin,
  password `GRAFANA_ADMIN_PASSWORD` in `.env`.
- **Images are pinned:** `prom/prometheus:v3.5.0`, `grafana/grafana:12.1.1`,
  `danielqsj/kafka-exporter:v1.9.0`.

## Consequences
- `make up` starts Prometheus (9090), Grafana (3001) and kafka-exporter. Commands:
  - `make grafana` prints the URLs.
  - `make prom-alerts` prints the firing alerts.
  - `make prom-test` checks the config and runs the alert unit tests.
- A service run outside Compose (for example `make agent`) is not scraped, and
  ServiceDown fires for its Compose job.
- Evaluation scripts that run the services' code in-process (ml/) increase the
  in-process counters only. They never reach Prometheus. LiteLLM's spend metric still
  counts them.
- **Verified locally (2026-10-22):**
  - all 8 scrape targets are up;
  - 4 documents through the pipeline produced the expected counters (received,
    classified, 3 orders posted touchless, LLM cost per service, route templates on the
    ERP mock);
  - all 23 dashboard queries run;
  - stopping kafka-exporter fired ServiceDown, and restarting it resolved the alert.
