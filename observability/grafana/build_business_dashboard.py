"""Builds the Grafana dashboard "O2C Business" from the dbt marts (ADR-013).

Usage: uv run python observability/grafana/build_business_dashboard.py
Writes observability/grafana/dashboards/o2c-business.json. Every panel is SQL on
schema analytics through the read-only datasource "O2C Analytics" (grafana_ro); the
marts are rebuilt by `make dbt-build`. Change panels here, not in the Grafana UI.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "o2c-business.json"
DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "o2c-analytics"}
PIPELINE_KEYS = "('classifier', 'extractor', 'agent')"


def sql(raw: str, fmt: str = "table", ref: str = "A") -> dict:
    return {
        "datasource": DATASOURCE,
        "rawSql": " ".join(raw.split()),
        "format": fmt,
        "rawQuery": True,
        "editorMode": "code",
        "refId": ref,
    }


def panel(kind, title, grid, targets, unit=None, description="", **options) -> dict:
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
        "options": options,
    }


REDUCE_ALL = {"calcs": ["lastNotNull"], "fields": "", "values": True}


def stat(title, raw, x, unit, description="", decimals=0) -> dict:
    p = panel(
        "stat",
        title,
        (x, 0, 6, 4),
        [sql(raw)],
        unit,
        description,
        reduceOptions={"calcs": ["lastNotNull"], "fields": "", "values": False},
        colorMode="value",
        graphMode="none",
        textMode="value",
    )
    p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def build() -> dict:
    panels = [
        # --- row 1: the time range at a glance ---
        stat(
            "Orders",
            "select count(*) from analytics.fct_orders where $__timeFilter(extracted_at)",
            0,
            "short",
        ),
        stat(
            "Touchless rate",
            """select count(*) filter (where touchless)::numeric
                 / nullif(count(*) filter (where decision is not null), 0)
               from analytics.fct_orders where $__timeFilter(extracted_at)""",
            6,
            "percentunit",
            "Posted without any review decision / orders the agent decided.",
            decimals=1,
        ),
        stat(
            "Avg cycle time",
            """select avg(cycle_time_seconds) from analytics.fct_orders
               where $__timeFilter(extracted_at)""",
            12,
            "s",
            "From receipt of the document to the final decision (posted or rejected).",
        ),
        stat(
            "LLM cost per order",
            f"""select sum(cost_usd) filter (where key_alias in {PIPELINE_KEYS})
                  / nullif((select count(*) from analytics.fct_orders
                            where $__timeFilter(extracted_at)), 0)
                from analytics.mart_llm_cost where $__timeFilter(date_day)""",
            18,
            "currencyUSD",
            "Cost of the classifier, extractor and agent keys / orders.",
            decimals=4,
        ),
        # --- row 2: quality ---
        panel(
            "barchart",
            "Touchless rate per day",
            (0, 4, 12, 8),
            [
                sql(
                    """select date_day::timestamp as time, touchless_rate
                       from analytics.mart_daily_kpis
                       where $__timeFilter(date_day) order by 1""",
                    "time_series",
                )
            ],
            "percentunit",
            "Touchless orders / orders the agent decided, per day (UTC).",
            xTickLabelRotation=-45,
        ),
        panel(
            "piechart",
            "Orders by final status",
            (12, 4, 6, 8),
            [
                sql(
                    """select final_status, count(*) as orders
                       from analytics.fct_orders where $__timeFilter(extracted_at)
                       group by final_status order by orders desc"""
                )
            ],
            "short",
            reduceOptions=REDUCE_ALL,
            pieType="donut",
            legend={"displayMode": "table", "placement": "right", "values": ["value"]},
        ),
        panel(
            "bargauge",
            "Top exception reasons",
            (18, 4, 6, 8),
            [
                sql(
                    """select issue_code, sum(issue_count) as issues
                       from analytics.mart_exception_reasons
                       where $__timeFilter(date_day)
                       group by issue_code order by issues desc limit 10"""
                )
            ],
            "short",
            "Issues found by the agent's checks (one order can have several).",
            reduceOptions=REDUCE_ALL,
            orientation="horizontal",
            displayMode="basic",
        ),
        # --- row 3: speed and cost ---
        panel(
            "timeseries",
            "Cycle time per day (avg and p90)",
            (0, 12, 8, 8),
            [
                sql(
                    """select date_day::timestamp as time,
                         avg_cycle_time_seconds as "avg",
                         p90_cycle_time_seconds as "p90"
                       from analytics.mart_daily_kpis
                       where $__timeFilter(date_day) order by 1""",
                    "time_series",
                )
            ],
            "s",
        ),
        panel(
            "timeseries",
            "LLM cost per order per day",
            (8, 12, 8, 8),
            [
                sql(
                    f"""select date_day::timestamp as time,
                          sum(cost_usd) filter (where key_alias in {PIPELINE_KEYS})
                            / nullif(max(orders), 0) as cost_per_order
                        from analytics.mart_llm_cost
                        where $__timeFilter(date_day)
                        group by date_day order by 1""",
                    "time_series",
                )
            ],
            "currencyUSD",
        ),
        panel(
            "timeseries",
            "LLM cost per service per day",
            (16, 12, 8, 8),
            [
                sql(
                    """select date_day::timestamp as time, key_alias as metric,
                         sum(cost_usd) as cost
                       from analytics.mart_llm_cost
                       where $__timeFilter(date_day)
                       group by date_day, key_alias order by 1""",
                    "time_series",
                )
            ],
            "currencyUSD",
            "Per LiteLLM key (ADR-011): classifier, extractor, agent; eval and master "
            "are evaluation and admin calls.",
        ),
        # --- row 4: people and orders ---
        panel(
            "table",
            "Review decisions by action and reviewer",
            (0, 20, 8, 9),
            [
                sql(
                    """select reviewer, action, count(*) as decisions,
                         round(avg(seconds_to_decision) / 60) as avg_minutes_to_decision
                       from analytics.mart_reviews where $__timeFilter(decided_at)
                       group by reviewer, action order by decisions desc"""
                )
            ],
            description="From the append-only review audit trail.",
        ),
        panel(
            "table",
            "Latest 20 orders",
            (8, 20, 16, 9),
            [
                sql(
                    """select extracted_at, po_number, customer_name, customer_number,
                         total_amount, currency, final_status, touchless, issue_codes,
                         round(cycle_time_seconds) as cycle_time_s
                       from analytics.fct_orders
                       order by extracted_at desc limit 20"""
                )
            ],
        ),
    ]
    for number, p in enumerate(panels, start=1):
        p["id"] = number
    return {
        "uid": "o2c-business",
        "title": "O2C Business",
        "tags": ["o2c", "dbt"],
        "timezone": "utc",
        "schemaVersion": 41,
        "version": 1,
        "editable": False,
        "refresh": "5m",
        "time": {"from": "now-30d", "to": "now"},
        "panels": panels,
        "templating": {"list": []},
        "annotations": {"list": []},
    }


def main() -> None:
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
