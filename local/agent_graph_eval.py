import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from agent.anomaly import anomaly_score
from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.graph import Deps, build_graph, run
from agent.materials import make_material_matcher
from agent.settings import Settings
from agent.state import AgentState
from langgraph.checkpoint.memory import InMemorySaver
from o2c_common.schema import PurchaseOrder


class DryRunErp(ErpClient):
    """Reads from the real ERP, but never creates or cancels real orders."""

    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.orders: dict[str, dict] = {}

    def create_order(self, order: dict) -> dict:
        vbeln = f"DRY{len(self.orders):07d}"
        total = sum(Decimal(i["kwmeng"]) * Decimal(i["netpr"]) for i in order["items"])
        self.orders[vbeln] = {
            "vbeln": vbeln,
            "status": "OPEN",
            "items": order["items"],
            "netwr": str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }
        return self.orders[vbeln]

    def get_order(self, vbeln: str) -> dict | None:
        return self.orders.get(vbeln)

    def cancel_order(self, vbeln: str) -> dict:
        self.orders[vbeln]["status"] = "CANCELLED"
        return self.orders[vbeln]


s = Settings()
deps = Deps(
    erp=DryRunErp(s.erp_base_url),
    tolerance=make_tolerance_lookup(s, make_store(s)),
    match_material=make_material_matcher(s),
    anomaly=anomaly_score,
    explain=lambda state: "dry run",
    auto_post_limit=s.auto_post_limit,
)
graph = build_graph(deps, InMemorySaver())
labels = json.loads(Path("poc/data/labels.json").read_text(encoding="utf-8"))

correct, outcomes, issue_counts = 0, Counter(), Counter()
for gt in sorted(Path("poc/data/ground_truth").glob("*.json")):
    po = PurchaseOrder.model_validate_json(gt.read_text(encoding="utf-8"))
    key = f"eval-{gt.stem}".ljust(16, "0")
    state = run(
        graph,
        AgentState(
            idempotency_key=key,
            correlation_id=key,
            source_event_id="eval",
            purchase_order=po,
        ),
        {"configurable": {"thread_id": key}},
    )
    flagged = sorted({i.line_no for i in state.issues if i.code == "PRICE_DEVIATION"})
    label = labels[gt.stem]
    expected = [label["line_no"]] if label.get("price_deviation") else []
    ok = flagged == expected
    correct += ok
    outcomes[state.status] += 1
    issue_counts.update(i.code.value for i in state.issues)
    print(
        f"{gt.stem}  {state.status:15}  {'OK ' if ok else 'BAD'}  {[i.code.value for i in state.issues]}"
    )

print(f"\nPrice check correct: {correct}/50")
print("Outcomes:", dict(outcomes))
print("Issues:", dict(issue_counts))
