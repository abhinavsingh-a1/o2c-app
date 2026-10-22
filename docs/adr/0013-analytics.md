# ADR-013: Business analytics: events -> raw -> dbt marts

Date: 2026-10-22
Status: Accepted

## Context
Prometheus (ADR-012) answers "is the pipeline healthy now?" with counters that are
reset at every restart and kept for 15 days. Finance and operations ask different
questions, over months and per order:

- How many orders went through without a person?
- Why did the others stop, and how long did they wait?
- Who reviewed them, and what does an order cost in LLM calls?

The answers are spread over Kafka events, the agent's review audit trail
(`public.review_decisions`) and LiteLLM's spend logs, in two databases.

## Decision

### events -> raw -> dbt marts
1. **`services/analytics-sink`** copies the facts unchanged into schema `raw` of the
   o2c database:
   - every pipeline event (Envelope) into `raw.events`;
   - every DLQ message, as its raw value plus headers, into `raw.dlq`;
   - every 60 s, LiteLLM's spend log rows (request id, start time, spend, model, model
     group, key alias) into `raw.llm_spend`.
2. **dbt (`analytics/dbt`)** turns `raw` into staging views (schema `staging`, one per
   event type) and the marts (tables in schema `analytics`).
3. **Grafana** reads only `analytics`.

Why this split:
- **Events are the system of record of what happened, and when.** The services' own
  tables only hold current state: `review_queue` is overwritten, and the agent's
  checkpoints are internal. Keeping the events unchanged in `raw` lets new questions be
  answered for the past, and lets a fixed mart be rebuilt with `dbt build`.
- **Transformations live in versioned SQL with tests**, not in dashboard queries.
  Grafana shows the same numbers that dbt tests (unique keys, accepted statuses,
  relationships, `touchless_rate` between 0 and 1, unit tests of the rules).
- **One database, no new infrastructure.** Postgres already holds the o2c data and the
  volume is small. A warehouse (BigQuery or Snowflake) would only change the dbt
  adapter and the sink's target.

### Idempotency
- **Events:** the sink uses `o2c_common.kafka.EventConsumer` (group `analytics-sink`),
  whose processed-events store skips redeliveries. On top of that, every write is
  `INSERT ... ON CONFLICT DO NOTHING` on the natural key:
  - `raw.events.event_id`;
  - `raw.dlq` (topic, partition, offset);
  - `raw.llm_spend.request_id`.

  So a redelivered event, a lost processed-events mark or a re-copied spend row never
  creates a second row. Events that are in Kafka twice (same event id) were seen
  locally and are skipped.
- **The DLQ** uses `RawMessageConsumer`, an `EventConsumer` for messages that may not be
  valid Envelopes (separate group `analytics-sink-dlq`). It never writes to the DLQ
  itself, which would loop: a failing message is retried with backoff until it
  succeeds.
- **The spend copy is incremental.** Its watermark is LiteLLM's insert time
  (`created_at`), because LiteLLM writes spend logs in batches. Each run re-reads the
  last 5 minutes, and the primary key drops the rows already copied.
- **The marts are rebuilt from `raw`** by every `dbt build` (`table` materialization),
  so they are idempotent by construction.

### Read-only BI user
At start, the sink creates the schemas `raw` and `analytics` and the role `grafana_ro`:
- **Password:** `GRAFANA_DB_PASSWORD` in `.env`; with the `replace-me` placeholder the
  role is not created.
- **Privileges:** `SELECT` on schema `analytics` only, including tables dbt creates
  later, through default privileges for the database user dbt runs as.
- **Read-only:** `default_transaction_read_only = on`.
- **No access** to `raw` (payloads with names and addresses), `staging`, `public`
  (reviews, checkpoints, audit trail) or the other databases.
- **Grafana:** the datasource "O2C Analytics" uses this role. A wrong panel or a leaked
  Grafana session can at most read the marts.

Verified: `grafana_ro` reads `analytics.fct_orders`. It gets "permission denied" on
`raw.events` and `public.review_decisions`, and "read-only transaction" on
`CREATE TABLE`.

### What each mart answers

| Mart | Grain | Question |
|---|---|---|
| `fct_documents` | document (idempotency key) | What arrived, when, and what was it (doc type, DistilBERT or GPT, routing reason)? |
| `fct_orders` | order with an extracted PO | What did the agent decide and what is the order's status now? When was it posted or first sent to review, how long did it take end to end, which issues did it have, and was it touchless? |
| `mart_daily_kpis` | day (UTC) | Documents and orders per day, orders by current status, touchless rate, average and p90 cycle time. |
| `mart_exception_reasons` | day and issue code | Why do orders stop: count, orders affected, and share of the day's issues per check? |
| `mart_reviews` | review decision | Who decided what (approve, edit, reject), and how long did the order wait from the first exception? |
| `mart_llm_cost` | day, key alias, model | What do the LLM calls cost per service and model, and per order? Only the classifier, extractor and agent keys count as order cost; eval and master-key calls are shown separately. |

### Definitions
- **Agent decision vs. after review.** The agent's own decision is the `orders.validated`
  event caused by `orders.extracted`. Events emitted after a review decision have no
  `causation_id` (`outcome_events(after, None)`).
- **Touchless:** the order is posted, and there is no review decision for it (no audit
  row and no post-review event).
- **Touchless rate:** touchless orders ÷ orders the agent decided. This is the same
  definition as the Prometheus alert (ADR-012).
- **Cycle time:** from `docs.received` to the final decision (posted or rejected), in
  whole seconds. It is NULL while the order is open.

### Tooling
- **dbt-postgres** is in the dependency group `analytics`. `profiles.yml` lives in the
  project and reads the connection from environment variables; `make dbt-build` loads
  `.env`.
  - `make dbt-build` runs `dbt source freshness`, which only warns when `raw.events` has
    no new rows for 24 h, and then `dbt build`.
  - `make dbt-docs` serves the docs and lineage on port 8580.
- **dbt-core is 1.8.7**, the newest release compatible with this workspace. kfp 2.13
  needs protobuf < 5 and mlflow-skinny 2.17.2 needs protobuf < 6; dbt-core 1.10+ needs
  protobuf ≥ 6.
  - dbt 1.8 is out of support. Upgrade when the ML pins move, or move dbt to its own
    environment (e.g. a separate uv project or image) if that takes long.
- **sqlfluff** (pre-commit hook and CLI) uses `analytics/dbt/.sqlfluff`: the jinja
  templater with `apply_dbt_builtins`, so `ref()`, `source()` and `config()` are
  rendered as stubs and no database or dbt install is needed.
- **Grafana:** dashboard "O2C Business" (`build_business_dashboard.py`; every panel is
  SQL on `analytics.*`).

## Consequences
- **Marts are as fresh as the last `dbt build`.** It is run manually for now; a
  scheduled run (Kubernetes CronJob or Airflow) comes with the cluster (E14). The sink
  itself is near real-time.
- **History is limited by Kafka retention.** The sink reads the topics from the
  earliest offset, so events older than the retention (7 days by default) before the
  sink started are not in `raw`. Reviews of such orders have no `fct_orders` row,
  which is why that relationship test is a warning.
- **`raw` grows by one row per event.** Retention and partitioning are not needed at
  the demo volume; revisit with production volumes.
- **Every event schema change must keep the staging models working.** Staging is the
  only place that reads payload JSON.
- **Verified locally (2026-10-22):**
  - 106 events, 0 DLQ messages and about 3,100 spend rows copied;
  - `make dbt-build`: 54 of 54 (36 data tests, 3 unit tests, freshness PASS);
  - all 12 business panels run through Grafana as `grafana_ro`.

  `public.review_decisions` was empty, so the review panels show no rows yet. The unit
  tests cover their logic.

## Cost per order corrected (E15 Part C, 2026-10-22)
The dashboard showed an LLM cost per order of $0.00015, about 8x too low.
- **raw.llm_spend was complete:** the same 12 key/model groups, counts and spend as
  LiteLLM's table.
- **The attribution was wrong.** mart_llm_cost counted only calls made with the
  per-service keys (classifier, extractor, agent) as order cost. Those keys exist only
  since 2026-10-22 (ADR-011); earlier pipeline calls used the master key. The stat then
  divided one day's keyed cost by the orders of all days.
- **Calls that serve many orders** (contract tolerance lookup, material matching,
  embeddings) were counted per order as well.

Fix:
- **The sink also copies `session_id` and the calling service** (the first request tag
  that is not a User-Agent tag). The services send the order's idempotency key as the
  session of every call made for one document: classify, extract, explain.
- **Rows copied before these columns existed** were backfilled once. The watermark only
  counts complete rows, and an upsert fills the two columns.
- **`fct_orders.llm_cost_usd`** is the cost of the calls whose session is the order, eval
  key excluded. `mart_daily_kpis.llm_cost_per_order_usd` is its daily average, and the
  dashboard's cost per order uses these.
- **`mart_llm_cost` splits every call into one category:**
  - **order:** the session is an order;
  - **document:** the session is a received document that is not an order, e.g.
    classifying an invoice;
  - **shared:** agent calls with no document session;
  - **evaluation:** the eval key or tag, `eval-`/`redteam-`/`judge-` sessions, and
    classifier or extractor calls for no real document. These services only ever call
    the LLM for a document, so such calls come from evaluations made with the master
    key before the eval key existed.

  `cost_per_order_usd` = order cost / orders that day.

Result on the local data:

| Measure | Value |
|---|---|
| Average cost per order | $0.00121 (was $0.00015) |
| Range per order | $0.00028–$0.00228 |
| Order calls | $0.023 |
| Shared calls | $0.026 |
| Evaluations | $3.12 of the $3.17 total |

Shared cost is reported but not divided by orders. Covered by the dbt unit test
`cost_per_order_counts_only_calls_for_orders`.
