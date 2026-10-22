"""Static checks of the dbt project and the business dashboard (ADR-013). The models
themselves are tested by dbt (make dbt-build: data tests and unit tests)."""

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DBT = ROOT / "analytics" / "dbt"
GRAFANA = ROOT / "observability" / "grafana"
sys.path.insert(0, str(GRAFANA))

import build_business_dashboard


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def models(folder: str) -> set[str]:
    return {p.stem for p in (DBT / "models" / folder).glob("*.sql")}


def documented(folder: str, filename: str) -> dict[str, dict]:
    return {m["name"]: m for m in load(DBT / "models" / folder / filename)["models"]}


def test_profile_reads_every_connection_setting_from_the_environment():
    output = load(DBT / "profiles.yml")["o2c"]["outputs"]["local"]
    for field in ("host", "port", "user", "password", "dbname"):
        assert "env_var(" in str(output[field]), field
    assert output["schema"] == "analytics"


def test_staging_has_a_model_per_event_type():
    from analytics_sink.app import TOPICS

    staging = models("staging")
    for topic in TOPICS:
        assert f"stg_{topic.replace('.', '_')}" in staging, topic


def test_the_agreed_marts_exist_and_are_tested():
    marts = models("marts")
    assert marts == {
        "fct_documents",
        "fct_orders",
        "mart_daily_kpis",
        "mart_exception_reasons",
        "mart_reviews",
        "mart_llm_cost",
    }
    docs = documented("marts", "_marts.yml")
    assert set(docs) == marts
    for name, model in docs.items():
        tests = list(model.get("data_tests", [])) + [
            t for c in model.get("columns", []) for t in c.get("data_tests", [])
        ]
        assert tests, name


def test_touchless_rate_is_tested_between_0_and_1():
    kpis = documented("marts", "_marts.yml")["mart_daily_kpis"]
    rate = next(c for c in kpis["columns"] if c["name"] == "touchless_rate")
    assert {"between": {"min_value": 0, "max_value": 1}} in rate["data_tests"]


def test_raw_events_has_a_freshness_warning_only():
    sources = {
        s["name"]: s
        for s in load(DBT / "models" / "staging" / "_sources.yml")["sources"]
    }
    events = next(t for t in sources["raw"]["tables"] if t["name"] == "events")
    assert events["freshness"]["warn_after"] == {"count": 24, "period": "hour"}
    assert "error_after" not in events["freshness"]
    assert events["loaded_at_field"] == "ingested_at"


def test_marts_go_to_analytics_and_staging_elsewhere():
    config = load(DBT / "dbt_project.yml")["models"]["o2c"]
    assert config["marts"]["+schema"] == "analytics"
    assert config["staging"]["+schema"] == "staging"


def test_unit_tests_cover_the_touchless_rule():
    tests = load(DBT / "models" / "marts" / "_unit_tests.yml")["unit_tests"]
    assert {t["model"] for t in tests} >= {
        "fct_orders",
        "mart_daily_kpis",
        "mart_reviews",
    }


# --- Grafana "O2C Business" ---

DASHBOARD = json.loads(
    (GRAFANA / "dashboards" / "o2c-business.json").read_text("utf-8")
)


def test_business_dashboard_json_is_generated():
    assert DASHBOARD == build_business_dashboard.build()


def test_business_panels_only_read_existing_analytics_marts():
    marts = models("marts")
    for panel in DASHBOARD["panels"]:
        for target in panel["targets"]:
            tables = re.findall(
                r"\b(?:from|join)\s+([a-z_]+\.[a-z_]+)", target["rawSql"]
            )
            assert tables, panel["title"]
            for table in tables:
                schema, name = table.split(".")
                assert schema == "analytics" and name in marts, (panel["title"], table)


def test_business_dashboard_has_the_agreed_panels():
    titles = [p["title"] for p in DASHBOARD["panels"]]
    assert len(titles) == len(set(titles))
    for wanted in (
        "Touchless rate per day",
        "Orders by final status",
        "Top exception reasons",
        "Cycle time per day (avg and p90)",
        "LLM cost per order per day",
        "LLM cost per service per day",
        "Review decisions by action and reviewer",
        "Latest 20 orders",
    ):
        assert wanted in titles


@pytest.mark.parametrize("panel", DASHBOARD["panels"], ids=lambda p: p["title"])
def test_every_panel_uses_the_read_only_datasource(panel):
    datasource = load(GRAFANA / "provisioning" / "datasources" / "analytics.yml")[
        "datasources"
    ][0]
    assert datasource["user"] == "grafana_ro"
    assert datasource["secureJsonData"]["password"] == "$GRAFANA_DB_PASSWORD"
    assert panel["datasource"]["uid"] == datasource["uid"]
    assert all(t["datasource"]["uid"] == datasource["uid"] for t in panel["targets"])
