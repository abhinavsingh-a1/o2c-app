import sys
from pathlib import Path

# observability/prom_alerts.py and observability/grafana/build_dashboard.py are scripts.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "observability"))
sys.path.insert(0, str(ROOT / "observability" / "grafana"))
