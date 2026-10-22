# E15 Execution Guide — Observability, Analytics and Audit Trail

E15 was done in **fast mode** on `feat/e15-observability` (PR #36) in `o2c-app` and `feat/e15-audit` in `o2c-infra`. Claude Code implemented each part from a spec; this guide records results, decisions and every problem. Everything ran locally; cost $0 (the AWS audit module is written but not applied).

**Result:** an operations dashboard (Prometheus + Grafana) with alert rules, a business dashboard on tested dbt models, a hash-chained audit archive whose tampering is detectable, and a Terraform audit module ready for the final AWS session.

## Results

| Check | Result |
| --- | --- |
| Prometheus targets | 8 / 8 UP |
| `make dbt-build` | PASS=54 WARN=0 ERROR=0 (36 data tests, 3 unit tests) |
| Audit chain (`ml/verify_audit.py`) | Intact, 47 entries; restart resumed at seq 47 without duplicates |
| LLM cost per order | $0.00121 (after attribution fix; was shown as $0.00015) |
| `analytics.mart_reviews` | 1 row after a new UI decision (older decisions predate the audit table) |
| Saga finding | 3 `ORDER_VERIFY_FAILED` caused by ERP rounding → fixed |

| URL | What |
| --- | --- |
| http://localhost:9090 | Prometheus (targets, alerts) |
| http://localhost:3001 | Grafana: "O2C Operations" and "O2C Business" |
| http://localhost:8580 | dbt docs and lineage (`make dbt-docs`) |

## Part A — Metrics, Prometheus, Grafana, alerts (ADR-012)

- **Technical metrics:** HTTP count/latency per endpoint; events consumed, failed and sent to the DLQ per consumer group (inside `EventConsumer`, so every consumer has them); handler duration.
- **Business metrics:** `o2c_documents_received_total`, `o2c_documents_classified_total{doc_type, classified_by}`, `o2c_extraction_seconds`, `o2c_orders_total{outcome}`, `o2c_issues_total{code}`, `o2c_reviews_total{action}`, `o2c_llm_cost_usd_total{service}`.
- **/metrics** on every service; intake uses Prometheus multiprocess mode (gunicorn, 2 workers); the classifier exposes it on its health server.
- **Label rule:** no high-cardinality labels (never idempotency keys).
- **Compose:** `prometheus` (9090), `grafana` (3001; 3000 is Langfuse), `kafka-exporter` for consumer lag; pinned versions; `GRAFANA_ADMIN_PASSWORD` in `.env`.
- **Alert rules** (`observability/prometheus/alerts.yml`): service down 2 min, any DLQ message in 10 min, consumer lag > 50 for 5 min, touchless rate < 50% (≥ 5 orders, 1 h), LLM cost > $0.50 per hour, error rate > 5%.
- **Grafana as code** (`observability/grafana/`): datasource and dashboard "O2C Operations".
- Targets `make grafana`, `make prom-alerts`.

## Part B — Analytics sink, dbt, business dashboard (ADR-013)

- **`analytics-sink`** service: consumes all business topics into `raw.events` (idempotent by `event_id`), copies `orders.dlq` into `raw.dlq`, and every 60 s copies LiteLLM spend logs into `raw.llm_spend`. It creates schemas `raw` and `analytics` and the read-only role `grafana_ro` (SELECT on `analytics` only).
- **dbt project** `analytics/dbt` (dbt-postgres, connection from env vars):

| Model | Answers |
| --- | --- |
| `fct_documents` | What arrived, how it was classified and routed |
| `fct_orders` | One row per PO: decision, final status, touchless, cycle time, amount, issues |
| `mart_daily_kpis` | Documents, orders, posted / review / rejected, touchless rate, avg and p90 cycle time per day |
| `mart_exception_reasons` | Which issues cause manual work |
| `mart_reviews` | Time from exception to decision, action, reviewer |
| `mart_llm_cost` | LLM cost per day, service and model; cost per order |

- Tests: unique / not\_null keys, accepted values, relationships, touchless rate between 0 and 1, source freshness, plus 3 dbt unit tests (touchless only without any review; touchless counts agent decisions only; review wait from the first exception).
- **Cost attribution:** each LLM call is matched to its order by `session_id` (= idempotency key) and classed as order, document, shared or evaluation. Most of the $3.17 total LLM spend so far is evaluation ($3.12).
- Grafana dashboard **"O2C Business"** through the `grafana_ro` datasource. Targets `make dbt-build`, `make dbt-docs`.

## Part C — Fixes, audit archive, AWS audit module (ADR-014)

- **Rounding bug found by the saga:** a line like 168.5 × 1.85 = 311.725 was rounded to 311.72 by the ERP mock (Python's default half-even) and to 311.73 by the agent (half-up). `verify_order` saw one cent difference and the saga cancelled the orders. The ERP mock now rounds half-up, with a test using the real case.
- **`audit-archiver`** service: appends one JSON line per decision (`orders.validated`, `orders.posted`, `orders.exceptions`, and new rows in `review_decisions`) to `s3://o2c-audit/<yyyy>/<mm>/<dd>.jsonl`, idempotent, one chain across days. Each record holds `seq`, timestamp, source, idempotency key, the record, `prev_hash` and `hash` = SHA-256 over **all** fields plus `prev_hash` (the original spec hashed only the record, which would have let other fields change undetected).
- **`ml/verify_audit.py`** walks the chain and reports the first broken record (exit code 1). Tests: a changed field breaks at that entry, a recomputed hash at the next, a deletion where it is missing — also across days.
- **AWS audit module** (`o2c-infra/terraform/modules/audit`, wired into `envs/dev` behind `enable_audit = false`): CloudTrail management events to an encrypted bucket (7-day lifecycle, `force_destroy`); AWS Config recorder limited to EKS clusters, RDS instances, S3 buckets and IAM roles (cents per month); an archive bucket with **Object Lock in governance mode, 1-day retention**, so `terraform destroy` still works (AWS rule: everything must be destroyable). Formatted and validated only.
- `ml/check_env.py` reports unparsable `.env` lines by number and key without printing values; `.env.example` documents single quotes for special characters.

## Troubleshooting — problems hit during E15 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `Failed to parse environment file .env at position 9: <choose, min 8 chars>12` | Placeholder text left in a `.env` value; special characters break parsing and may drop later settings | Values with special characters in single quotes; `ml/check_env.py` |
| 2 | Three orders `ORDER_VERIFY_FAILED`, then cancelled | ERP mock rounded half-even, agent half-up | ERP mock rounds half-up; test with the half-cent case |
| 3 | `mart_reviews` empty | All earlier decisions were made before the audit table existed | Not invented; a new UI decision appears after `make dbt-build` |
| 4 | Cost per order about 8× too low | Only calls with per-service keys counted, divided by all orders | Attribution by `session_id`; backfill; dbt unit test |
| 5 | Hash chain allowed changing seq/timestamp | Hash covered only the record | Hash covers every field |
| 6 | `o2c-infra` branch looked deleted on GitHub | Nothing was committed on it; `terraform/` had never been committed | Checked for state files, `.terraform/`, `.tfvars`; committed and merged |
| 7 | pre-commit skipped Terraform hooks | Hooks run on tracked files only | Run hooks with `--files` before the first commit |
| 8 | Many red `remotes/origin/feat/...` branches | Old remote-tracking refs and branches not deleted after merge | `git fetch --prune`; delete merged remote branches with a loop checking `gh pr list --state merged` |
| 9 | Forgotten UI passwords | Only hashes are stored | `uv run python -m agent.auth add-user <user> "<Name>"` overwrites the password |
| 10 | UI says "Wrong username or password" although the API login works | nginx resolved the agent's address only at start; the restarted agent got a new address | `docker compose restart ui`; permanent fix: `resolver 127.0.0.11` with a variable `proxy_pass` in `ui/nginx.conf` |
| 11 | A real password was pasted into the chat | — | Rotate it; mask passwords as `***` before pasting |

**Lessons:**

- Business metrics belong next to technical ones; the touchless rate is the number the business cares about.
- Analytics in tested dbt models catch real bugs (the rounding issue).
- Cost attribution needs a join key (session id) from the start.
- A hash chain makes an audit log tamper-evident without special storage; Object Lock adds immutability on AWS.
- Containers that cache DNS break when a dependency restarts — re-resolve.
