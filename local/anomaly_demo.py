"""Quantity anomaly demo: the agent graph on a clean golden PO and on the same PO with one
line quantity multiplied by 20. Expect QUANTITY_ANOMALY only in the second run.

Usage: uv run python local/anomaly_demo.py [PO-ID]
Without PO-ID the first poc/data PO that the graph passes without any issue is used.

Needs the local stack (make up): the ERP mock API with history (make erp-seed-history),
LiteLLM and the contract index for price tolerances, and MLflow with
models:/o2c-quantity-anomaly@champion (ml/train_anomaly.py). The ERP is only read:
orders are "created" in memory (DryRunErp), as in local/agent_graph_eval.py.
"""

import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from agent import anomaly
from agent.anomaly import anomaly_score
from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.graph import Deps, build_graph, run
from agent.materials import make_material_matcher
from agent.settings import Settings
from agent.state import AgentState, IssueCode
from langgraph.checkpoint.memory import InMemorySaver
from o2c_common.schema import PurchaseOrder

GOLDEN = Path("poc/data/ground_truth")
CENT = Decimal("0.01")


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
            "netwr": str(total.quantize(CENT, rounding=ROUND_HALF_UP)),
        }
        return self.orders[vbeln]

    def get_order(self, vbeln: str) -> dict | None:
        return self.orders.get(vbeln)

    def cancel_order(self, vbeln: str) -> dict:
        self.orders[vbeln]["status"] = "CANCELLED"
        return self.orders[vbeln]


def times_20(po: PurchaseOrder) -> tuple[PurchaseOrder, int]:
    """Copy with the cheapest line's quantity x20; line and order totals recomputed."""
    target = min(po.lines, key=lambda line: line.line_total)
    lines = []
    for line in po.lines:
        if line.line_no == target.line_no:
            quantity = line.quantity * 20
            line = line.model_copy(
                update={
                    "quantity": quantity,
                    "line_total": (quantity * line.unit_price).quantize(
                        CENT, ROUND_HALF_UP
                    ),
                }
            )
        lines.append(line)
    total = sum((line.line_total for line in lines), Decimal(0))
    return po.model_copy(update={"lines": lines, "total_amount": total}), target.line_no


def process(graph, po: PurchaseOrder, key: str) -> AgentState:
    key = key.ljust(16, "0")
    state = AgentState(
        idempotency_key=key,
        correlation_id=key,
        source_event_id="demo",
        purchase_order=po,
    )
    return run(graph, state, {"configurable": {"thread_id": key}})


def show(title: str, state: AgentState) -> None:
    print(f"\n== {title}: status {state.status}")
    for check in state.lines:
        print(
            f"   line {check.line_no}: material {check.material_number}, anomaly score {check.anomaly_score}"
        )
    if not state.issues:
        print("   issues: none")
    for issue in state.issues:
        print(f"   {issue.code.value}: {issue.message}")


def main() -> int:
    if anomaly.get_model() is None:
        print(
            f"{anomaly.MODEL_URI} is not available; run ml/train_anomaly.py and start MLflow."
        )
        return 1
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

    candidates = (
        [GOLDEN / f"{sys.argv[1]}.json"]
        if len(sys.argv) > 1
        else sorted(GOLDEN.glob("*.json"))
    )
    for path in candidates:
        po = PurchaseOrder.model_validate_json(path.read_text(encoding="utf-8"))
        clean = process(graph, po, f"demo-{path.stem}")
        if not clean.issues:
            break
        print(
            f"{path.stem}: not clean ({', '.join(i.code.value for i in clean.issues)}), trying the next PO"
        )
    else:
        print("no golden PO passes without issues")
        return 1

    show(f"{path.stem} as received", clean)
    changed_po, line_no = times_20(po)
    changed = process(graph, changed_po, f"demo-{path.stem}-x20")
    show(f"{path.stem} with line {line_no} quantity x20", changed)

    codes = [{i.code for i in state.issues} for state in (clean, changed)]
    ok = (
        IssueCode.QUANTITY_ANOMALY not in codes[0]
        and IssueCode.QUANTITY_ANOMALY in codes[1]
    )
    print(
        f"\n{'OK' if ok else 'UNEXPECTED'}: QUANTITY_ANOMALY only in the second run: {ok}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
