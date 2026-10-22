# analytics-sink

Copies pipeline events, the DLQ and LiteLLM spend logs into the raw schema of the o2c
database, for the dbt marts in analytics/dbt (ADR-013).

Run locally: `uv run --package analytics-sink python -m analytics_sink.app` (Compose runs it with `make up`).
