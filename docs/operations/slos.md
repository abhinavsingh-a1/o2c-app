# Service level indicators and objectives

Last updated: 2026-10-28. Owner: Abhinav.

The objectives are for the local stack and the demo volume. They go into the Helm values
per environment with the cloud. Every query below already exists, in
`observability/prometheus/alerts.yml`, the "O2C Operations" dashboard
(`observability/grafana/build_dashboard.py`) or the dbt marts, unless it is marked
**new**. **New** queries are proposals that were not run. Alerts are unit-tested with
`make prom-test`.

| SLI | SLO | How it is measured | Alert (exists) |
|---|---|---|---|
| Availability | Every service scraped `up`: 99% of 5-minute windows over 30 days | `up` per job; `avg_over_time(up[30d])` (**new**) | `ServiceDown`: `up == 0` for 2m (critical) |
| Pipeline delivery | No document stuck: consumer lag <= 50; no DLQ message unhandled for more than 1 working day | `sum by (consumergroup) (kafka_consumergroup_lag)`; `sum(increase(kafka_topic_partition_current_offset{topic="orders.dlq"}[10m]))` | `ConsumerLagHigh` (> 50 for 5m); `DeadLetterQueueReceivedMessages` |
| API latency | p95 < 500 ms per service (HTTP) | `histogram_quantile(0.95, sum by (job, le) (rate(o2c_http_request_duration_seconds_bucket[5m])))` | None. Checked at promotion: k6 test p95 < 500 ms, prod SLO p95 < 800 ms (ADR-018); measured 8.5 ms (test) and 53.6 ms (prod). |
| Processing latency | p95 extraction < 30 s; p95 handler time < 60 s | `histogram_quantile(0.95, sum by (le) (rate(o2c_extraction_seconds_bucket[5m])))`; `histogram_quantile(0.95, sum by (group, le) (rate(o2c_event_handler_seconds_bucket[5m])))` | None (dashboard only) |
| Error rate | < 5% 5xx per service; < 5% failed handler attempts per consumer group | `sum by (job) (rate(o2c_http_requests_total{status=~"5.."}[5m])) / sum by (job) (rate(o2c_http_requests_total[5m]))`; `sum by (group) (rate(o2c_events_failed_total[5m])) / sum by (group) (rate(o2c_events_consumed_total[5m]))` | `HttpErrorRateHigh`, `EventErrorRateHigh` (5m) |
| **Touchless rate** (business KPI) | >= 50% per hour with >= 5 orders; target >= 80% per day | `(sum(increase(o2c_orders_total{decided_by="agent", outcome="posted"}[1h])) or vector(0)) / sum(increase(o2c_orders_total{decided_by="agent"}[1h]))`; daily: `analytics.mart_daily_kpis.touchless_rate` | `TouchlessRateLow` (< 50%, >= 5 orders) |
| Extraction quality | poc/data >= 48/50 perfect and >= 99.5% fields; holdout >= 9/20 and >= 98.8% | `make eval` against `eval/thresholds.yaml` at every promotion to test | Gate (exit 1) blocks the promotion |
| Classification quality | Hybrid holdout accuracy >= 97.5%; 0 false PO labels | Same gate | Gate |
| Explanation quality | Judge pass rate >= 90% (judge validation 7/7 first) | Same gate | Gate |
| Anomaly model working | 0 scoring fallbacks | `sum by (job, reason) (increase(o2c_anomaly_scoring_failures_total[10m]))`; gate step "anomaly model scores" | `AnomalyScoringFailing` (critical) |
| LLM cost per order | < $0.005 per order (daily average) | `analytics.mart_daily_kpis.llm_cost_per_order_usd` (dbt, ADR-013). Real-time proxy: `sum(increase(o2c_llm_cost_usd_total[1h])) / sum(increase(o2c_orders_total{decided_by="agent"}[1h]))` (**new**) | `LlmCostHigh`: > $0.50 per hour (services); LiteLLM monthly budgets per key (ADR-011) |

## Current values (2026-10-28, local)
| SLI | Value | Source |
|---|---|---|
| Extraction | poc/data 50/50, 100.00%; holdout 11/20, 99.19% | `make eval` (`eval/report.md`) |
| Classifier | 100.00%, 0 false POs | same |
| Explanations | 14/14 (judge validation 7/7) | same |
| Red team | 8/8 | same |
| Anomaly | normal 0.076, 20x 0.983, 0 errors | same |
| Cost per order | $0.00121 average ($0.00028 to $0.00228) | ADR-013 (2026-10-22) |
| Gate run cost | $0.1097 for 146 calls | `make eval` |
| Prod SLO check | p95 53.6 ms, 0% errors (promote-to-prod #6) | ADR-018 |

## Error budget policy (proposed)
- **Touchless rate below 80% for a day,** or **a gate threshold missed:** no model or
  prompt promotion until the cause is found. Use the issue codes
  (`o2c_issues_total{code}`, `analytics.mart_exception_reasons`).
- **Availability budget used up:** feature work stops for reliability work.
- **The SLO check fails after a prod promotion:** automatic rollback (ADR-018).

## Gaps
- No burn-rate alerts. All alerts are thresholds.
- No Alertmanager routing (ADR-012). Alerts are visible in Prometheus, Grafana and
  `make prom-alerts` only.
- The latency SLOs are measured through `kubectl port-forward`. They show regressions,
  not capacity.
