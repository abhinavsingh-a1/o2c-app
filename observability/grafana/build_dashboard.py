"""Builds the Grafana dashboard "O2C Operations" (ADR-012).

Usage: uv run python observability/grafana/build_dashboard.py
Writes observability/grafana/dashboards/o2c-operations.json, which Grafana loads at
startup (provisioning/dashboards/o2c.yml). Change panels here, not in the Grafana UI.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "o2c-operations.json"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}  # provisioning/datasources

# Share of orders the agent posted without a person, last hour (as the alert)
TOUCHLESS = (
    '(sum(increase(o2c_orders_total{decided_by="agent", outcome="posted"}[1h]))'
    " or vector(0))"
    ' / sum(increase(o2c_orders_total{decided_by="agent"}[1h]))'
)
DLQ_10M = 'sum(increase(kafka_topic_partition_current_offset{topic="orders.dlq"}[10m]))'
GREEN, RED, BLUE = "green", "red", "blue"


def query(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    target = {
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
    }
    if instant:
        target.update(instant=True, range=False)
    return target


def queries(*pairs: tuple[str, str]) -> list[dict]:
    return [
        query(expr, legend, chr(ord("A") + i)) for i, (expr, legend) in enumerate(pairs)
    ]


def panel(kind, title, grid, targets, unit=None, description="") -> dict:
    x, y, w, h = grid
    defaults = {"unit": unit} if unit else {}
    return {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }


def stat(title, expr, x, unit, steps, description="", decimals=0) -> dict:
    p = panel(
        "stat", title, (x, 0, 4, 4), [query(expr, instant=True)], unit, description
    )
    p["fieldConfig"]["defaults"].update(
        decimals=decimals,
        color={"mode": "thresholds"},
        thresholds={
            "mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in steps],
        },
    )
    p["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": "value",
    }
    return p


def timeseries(title, grid, targets, unit, description="") -> dict:
    return panel("timeseries", title, (*grid, 8), targets, unit, description)


def build() -> dict:
    panels = [
        # --- row 1: the numbers at a glance ---
        stat(
            "Touchless rate (1h)",
            TOUCHLESS,
            0,
            "percentunit",
            [(RED, None), (GREEN, 0.5)],
            "Orders the agent posted without a person / orders the agent decided, last "
            "hour. Alert below 50% with at least 5 orders.",
        ),
        stat(
            "Orders decided (1h)",
            'sum(increase(o2c_orders_total{decided_by="agent"}[1h]))',
            4,
            "short",
            [(BLUE, None)],
        ),
        stat(
            "Documents received (1h)",
            "sum(increase(o2c_documents_received_total[1h]))",
            8,
            "short",
            [(BLUE, None)],
        ),
        stat(
            "LLM cost (1h)",
            "sum(increase(o2c_llm_cost_usd_total[1h]))",
            12,
            "currencyUSD",
            [(GREEN, None), (RED, 0.5)],
            "Services only (x-litellm-response-cost). Alert above $0.50.",
            decimals=3,
        ),
        stat(
            "DLQ messages (10m)",
            f"{DLQ_10M} or vector(0)",
            16,
            "short",
            [(GREEN, None), (RED, 0.5)],
            "Messages that reached orders.dlq (kafka-exporter).",
        ),
        stat(
            "Firing alerts",
            'count(ALERTS{alertstate="firing"}) or vector(0)',
            20,
            "short",
            [(GREEN, None), (RED, 1)],
        ),
        # --- row 2: flow ---
        timeseries(
            "Documents per minute",
            (0, 4, 12),
            queries(
                (
                    "sum by (source) (rate(o2c_documents_received_total[5m])) * 60",
                    "received ({{source}})",
                ),
                (
                    "sum by (doc_type) (rate(o2c_documents_classified_total[5m])) * 60",
                    "classified: {{doc_type}}",
                ),
            ),
            "short",
            "Per minute, averaged over 5 minutes.",
        ),
        timeseries(
            "Orders per minute",
            (12, 4, 12),
            queries(
                (
                    "sum by (outcome, decided_by) (rate(o2c_orders_total[5m])) * 60",
                    "{{outcome}} ({{decided_by}})",
                )
            ),
            "short",
            "decided_by=agent: the agent's decision (posted = touchless); "
            "decided_by=reviewer: the outcome of a review.",
        ),
        # --- row 3: quality ---
        timeseries(
            "Touchless rate (1h window)",
            (0, 12, 8),
            queries((TOUCHLESS, "touchless")),
            "percentunit",
        ),
        exceptions_panel(),
        timeseries(
            "Reviews per hour by action",
            (16, 12, 8),
            queries(
                ("sum by (action) (increase(o2c_reviews_total[1h]))", "{{action}}")
            ),
            "short",
        ),
        # --- row 4: Kafka ---
        timeseries(
            "Consumer lag per group",
            (0, 20, 8),
            queries(
                (
                    "sum by (consumergroup) (kafka_consumergroup_lag)",
                    "{{consumergroup}}",
                )
            ),
            "short",
            "Messages not processed yet (kafka-exporter). Alert above 50 for 5 minutes.",
        ),
        timeseries(
            "DLQ",
            (8, 20, 8),
            queries(
                (DLQ_10M, "orders.dlq (10m)"),
                (
                    "sum by (group, reason) (increase(o2c_events_dlq_total[10m])) > 0",
                    "{{group}}: {{reason}}",
                ),
            ),
            "short",
            "Messages sent to the dead-letter queue, by consumer group and reason.",
        ),
        timeseries(
            "Events per minute",
            (16, 20, 8),
            queries(
                (
                    "sum by (group) (rate(o2c_events_consumed_total[5m])) * 60",
                    "consumed: {{group}}",
                ),
                (
                    "sum by (group) (rate(o2c_events_failed_total[5m])) * 60",
                    "failed attempts: {{group}}",
                ),
            ),
            "short",
        ),
        # --- row 5: latency and cost ---
        timeseries(
            "p95 HTTP latency per service",
            (0, 28, 8),
            queries(
                (
                    (
                        "histogram_quantile(0.95, sum by (job, le) "
                        "(rate(o2c_http_request_duration_seconds_bucket[5m])))"
                    ),
                    "{{job}}",
                )
            ),
            "s",
        ),
        timeseries(
            "p95 event handling and extraction",
            (8, 28, 8),
            queries(
                (
                    (
                        "histogram_quantile(0.95, sum by (group, le) "
                        "(rate(o2c_event_handler_seconds_bucket[5m])))"
                    ),
                    "handler: {{group}}",
                ),
                (
                    (
                        "histogram_quantile(0.95, sum by (le) "
                        "(rate(o2c_extraction_seconds_bucket[5m])))"
                    ),
                    "extraction (LLM)",
                ),
            ),
            "s",
        ),
        timeseries(
            "LLM cost per service (1h window)",
            (16, 28, 8),
            queries(
                (
                    "sum by (service) (increase(o2c_llm_cost_usd_total[1h]))",
                    "{{service}}",
                ),
                (
                    "sum(increase(litellm_spend_metric_total[1h]))",
                    "all LiteLLM callers (incl. evaluations)",
                ),
            ),
            "currencyUSD",
            "Services: x-litellm-response-cost of each call. LiteLLM's own spend "
            "metric also counts evaluation runs.",
        ),
        alerts_panel(),
    ]
    for number, p in enumerate(panels, start=1):
        p["id"] = number
    return {
        "uid": "o2c-operations",
        "title": "O2C Operations",
        "tags": ["o2c"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
        "templating": {"list": []},
        "annotations": {"list": []},
    }


def exceptions_panel() -> dict:
    p = panel(
        "bargauge",
        "Exceptions by issue code (time range)",
        (8, 12, 8, 8),
        [
            query(
                "sort_desc(sum by (code) (increase(o2c_issues_total[$__range])) > 0)",
                "{{code}}",
                instant=True,
            )
        ],
        "short",
        "Issues found by the agent's checks; one order can have several.",
    )
    p["options"] = {
        "orientation": "horizontal",
        "displayMode": "basic",
        "showUnfilled": True,
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    }
    return p


def alerts_panel() -> dict:
    target = query('ALERTS{alertstate="firing"}', instant=True)
    target["format"] = "table"
    p = panel(
        "table",
        "Firing alerts (details)",
        (0, 36, 24, 7),
        [target],
        description="Prometheus alert rules (observability/prometheus/alerts.yml); "
        "also: make prom-alerts.",
    )
    p["transformations"] = [
        {
            "id": "organize",
            "options": {
                "excludeByName": {
                    "Time": True,
                    "Value": True,
                    "__name__": True,
                    "alertstate": True,
                },
                "indexByName": {"alertname": 0, "severity": 1, "job": 2},
            },
        }
    ]
    return p


def main() -> None:
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
