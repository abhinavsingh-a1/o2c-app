"""Checks of the observability configuration as code (ADR-012): every metric a rule or
panel uses exists in o2c_common.metrics, labels stay within the cardinality rule, the
dashboard JSON is generated, and the Compose services are pinned and wired."""

import json
import re
from pathlib import Path

import build_dashboard
import prom_alerts
import pytest
import yaml
from o2c_common import metrics
from prometheus_client import Counter, Histogram

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "observability"
ALERTS = yaml.safe_load((OBS / "prometheus" / "alerts.yml").read_text(encoding="utf-8"))
PROMETHEUS = yaml.safe_load(
    (OBS / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
)
DASHBOARD = json.loads(
    (OBS / "grafana" / "dashboards" / "o2c-operations.json").read_text(encoding="utf-8")
)
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

O2C_NAME = re.compile(r"\bo2c_[a-z_]+")
BY_LABELS = re.compile(r"\bby \(([^)]*)\)")


def rules() -> dict[str, dict]:
    return {r["alert"]: r for group in ALERTS["groups"] for r in group["rules"]}


def dashboard_exprs() -> list[str]:
    return [t["expr"] for p in DASHBOARD["panels"] for t in p["targets"]]


def defined_series() -> set[str]:
    """Series names the o2c metrics expose (counters: _total; histograms: _bucket...)."""
    names = set()
    for m in vars(metrics).values():
        if isinstance(m, Counter):
            names.add(f"{m._name}_total")
        elif isinstance(m, Histogram):
            names |= {f"{m._name}_{s}" for s in ("bucket", "count", "sum")}
    return names


@pytest.mark.parametrize("source", ["alerts", "dashboard"])
def test_every_o2c_metric_used_exists(source):
    exprs = (
        [r["expr"] for r in rules().values()]
        if source == "alerts"
        else dashboard_exprs()
    )
    used = {name for expr in exprs for name in O2C_NAME.findall(expr)}
    assert used, source
    assert used <= defined_series(), used - defined_series()


def test_aggregations_only_use_low_cardinality_labels():
    # job, kafka-exporter's consumergroup and Prometheus' le are bounded as well
    allowed = metrics.ALLOWED_LABELS | {"job", "consumergroup", "le"}
    exprs = [r["expr"] for r in rules().values()] + dashboard_exprs()
    for expr in exprs:
        for group in BY_LABELS.findall(expr):
            labels = {label.strip() for label in group.split(",")}
            assert labels <= allowed, (expr, labels)


def test_the_agreed_alerts():
    r = rules()
    assert set(r) == {
        "ServiceDown",
        "DeadLetterQueueReceivedMessages",
        "ConsumerLagHigh",
        "TouchlessRateLow",
        "LlmCostHigh",
        "HttpErrorRateHigh",
        "EventErrorRateHigh",
        "AnomalyScoringFailing",
    }
    assert (r["ServiceDown"]["expr"], r["ServiceDown"]["for"]) == ("up == 0", "2m")
    assert "[10m]" in r["DeadLetterQueueReceivedMessages"]["expr"]
    assert "> 50" in r["ConsumerLagHigh"]["expr"]
    assert r["ConsumerLagHigh"]["for"] == "5m"
    touchless = r["TouchlessRateLow"]["expr"]
    assert "< 0.5" in touchless and ">= 5" in touchless and "[1h]" in touchless
    assert "or vector(0)" in touchless  # no posted order at all is 0%, not "no data"
    assert "> 0.5" in r["LlmCostHigh"]["expr"] and "[1h]" in r["LlmCostHigh"]["expr"]
    for name in ("HttpErrorRateHigh", "EventErrorRateHigh"):
        assert "> 0.05" in r[name]["expr"]
    anomaly = r["AnomalyScoringFailing"]  # any 0.0 fallback in 10 minutes, at once
    assert "[10m]" in anomaly["expr"] and "> 0" in anomaly["expr"]
    assert "for" not in anomaly and anomaly["labels"]["severity"] == "critical"
    for rule in r.values():
        assert rule["labels"]["severity"] in {"critical", "warning"}
        assert rule["annotations"]["summary"]


def test_every_alert_has_a_promtool_test():
    tests = yaml.safe_load(
        (OBS / "prometheus" / "alerts_test.yml").read_text(encoding="utf-8")
    )
    tested = {
        case["alertname"]
        for test in tests["tests"]
        for case in test.get("alert_rule_test", [])
    }
    assert tested == set(rules())


def test_prometheus_scrapes_every_service_litellm_and_kafka_exporter():
    jobs = {c["job_name"]: c for c in PROMETHEUS["scrape_configs"]}
    targets = {j: c["static_configs"][0]["targets"][0] for j, c in jobs.items()}
    assert targets == {
        "prometheus": "localhost:9090",
        "intake": "intake:8200",
        "classifier": "classifier:8301",
        "extractor": "extractor:8300",
        "agent": "agent:8400",
        "erp-mock": "erp-mock:8100",
        "analytics-sink": "analytics-sink:8310",
        "audit-archiver": "audit-archiver:8320",
        "litellm": "litellm:4000",
        "kafka-exporter": "kafka-exporter:9308",
    }
    assert jobs["litellm"]["metrics_path"] == "/metrics/"
    assert PROMETHEUS["rule_files"] == ["/etc/prometheus/alerts.yml"]


def test_dashboard_json_is_generated_from_build_dashboard():
    assert DASHBOARD == build_dashboard.build()  # run build_dashboard.py after a change


def test_dashboard_has_the_agreed_panels_on_the_provisioned_datasource():
    datasource = yaml.safe_load(
        (OBS / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(
            encoding="utf-8"
        )
    )["datasources"][0]
    titles = [p["title"] for p in DASHBOARD["panels"]]
    assert DASHBOARD["title"] == "O2C Operations"
    assert len(titles) == len(set(titles))
    for wanted in (
        "Documents per minute",
        "Orders per minute",
        "Touchless rate (1h)",
        "Exceptions by issue code (time range)",
        "Consumer lag per group",
        "DLQ",
        "p95 HTTP latency per service",
        "LLM cost per service (1h window)",
        "Firing alerts (details)",
    ):
        assert wanted in titles
    for p in DASHBOARD["panels"]:
        assert p["datasource"]["uid"] == datasource["uid"]
        assert all(t["datasource"]["uid"] == datasource["uid"] for t in p["targets"])


def test_compose_pins_and_wires_the_observability_services():
    services = COMPOSE["services"]
    for name in ("prometheus", "grafana", "kafka-exporter"):
        image = services[name]["image"]
        assert re.search(r":v?\d+\.\d+\.\d+$", image), image  # pinned, not :latest
    grafana = services["grafana"]
    assert grafana["ports"] == ["3001:3000"]
    assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD"] == (
        "${GRAFANA_ADMIN_PASSWORD}"
    )
    assert services["prometheus"]["ports"] == ["9090:9090"]
    intake = services["intake"]
    assert "intake.gunicorn_conf" in intake["command"]
    assert intake["environment"]["PROMETHEUS_MULTIPROC_DIR"]
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GRAFANA_ADMIN_PASSWORD=replace-me" in env_example


# --- make prom-alerts ---


def alert(name, state="firing", severity="warning", **labels) -> dict:
    return {
        "labels": {"alertname": name, "severity": severity, **labels},
        "annotations": {"summary": f"{name} summary"},
        "state": state,
        "activeAt": "2026-10-22T09:00:00.123Z",
    }


def test_report_lists_firing_alerts_critical_first():
    text = prom_alerts.report(
        [
            alert("ConsumerLagHigh", consumergroup="agent"),
            alert(
                "ServiceDown", severity="critical", job="agent", instance="agent:8400"
            ),
            alert("LlmCostHigh", state="pending"),
        ]
    ).splitlines()

    assert text == [
        (
            "ServiceDown [critical] (job=agent) since 2026-10-22 09:00:00 UTC "
            "- ServiceDown summary"
        ),
        (
            "ConsumerLagHigh [warning] (consumergroup=agent) since 2026-10-22 09:00:00 UTC "
            "- ConsumerLagHigh summary"
        ),
        "2 firing, 1 pending",
    ]


def test_report_without_firing_alerts():
    assert prom_alerts.report([]) == "no firing alerts\n0 firing, 0 pending"


def test_fetch_alerts_reads_the_prometheus_api(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"status": "success", "data": {"alerts": [alert("ServiceDown")]}}
            ).encode()

    urls = []
    monkeypatch.setattr(
        prom_alerts.urllib.request,
        "urlopen",
        lambda url, timeout: urls.append(url) or Response(),
    )

    alerts = prom_alerts.fetch_alerts("http://prom:9090")

    assert urls == ["http://prom:9090/api/v1/alerts"]
    assert alerts[0]["labels"]["alertname"] == "ServiceDown"


def test_main_reports_an_unreachable_prometheus(monkeypatch, capsys):
    def refuse(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(prom_alerts.urllib.request, "urlopen", refuse)

    assert prom_alerts.main() == 1
    assert "not reachable" in capsys.readouterr().out
