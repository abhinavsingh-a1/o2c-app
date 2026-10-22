"""Print the firing Prometheus alerts (make prom-alerts, ADR-012).

Usage: uv run python observability/prom_alerts.py
Reads GET /api/v1/alerts from PROMETHEUS_URL (default http://localhost:9090) and prints
one line per firing alert (name, severity, the labels that say where, since when and
the summary), then how many are pending (condition true, `for` not yet elapsed).
Exits 0 whether or not alerts fire (it reports, it does not gate); 1 only if
Prometheus cannot be reached.
"""

import json
import os
import sys
import urllib.request

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
# Labels already shown in their own column, or the same on every alert
HIDDEN_LABELS = {"alertname", "severity", "instance"}


def fetch_alerts(url: str = PROMETHEUS_URL, timeout: float = 10.0) -> list[dict]:
    with urllib.request.urlopen(f"{url}/api/v1/alerts", timeout=timeout) as response:
        data = json.load(response)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus answered {data.get('status')}: {data}")
    return data["data"]["alerts"]


def alert_line(alert: dict) -> str:
    labels = alert.get("labels", {})
    where = ", ".join(
        f"{k}={v}" for k, v in sorted(labels.items()) if k not in HIDDEN_LABELS
    )
    since = (alert.get("activeAt") or "")[:19].replace("T", " ")
    summary = alert.get("annotations", {}).get("summary", "")
    parts = [
        labels.get("alertname", "?"),
        f"[{labels.get('severity', 'none')}]",
        f"({where})" if where else "",
        f"since {since} UTC" if since else "",
        f"- {summary}" if summary else "",
    ]
    return " ".join(p for p in parts if p)


def report(alerts: list[dict]) -> str:
    firing = sorted(
        (a for a in alerts if a.get("state") == "firing"),
        key=lambda a: (
            a["labels"].get("severity") != "critical",
            a["labels"]["alertname"],
        ),
    )
    pending = sum(1 for a in alerts if a.get("state") == "pending")
    lines = [alert_line(a) for a in firing] or ["no firing alerts"]
    lines.append(f"{len(firing)} firing, {pending} pending")
    return "\n".join(lines)


def main() -> int:
    try:
        alerts = fetch_alerts()
    except OSError as exc:
        print(f"Prometheus not reachable at {PROMETHEUS_URL}: {exc} (make up?)")
        return 1
    print(report(alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
