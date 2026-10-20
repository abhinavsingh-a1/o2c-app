import json
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from agent.anomaly import anomaly_score
from agent.checkpoint import postgres_checkpointer, resume_with_review, run_or_resume
from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.explain import make_explainer
from agent.graph import Deps, build_graph
from agent.materials import make_material_matcher
from agent.settings import Settings
from agent.state import AgentState, IssueCode, Review
from o2c_common.schema import PurchaseOrder

s = Settings()
labels = json.loads(Path("poc/data/labels.json").read_text(encoding="utf-8"))
deviating = [
    po
    for gt in sorted(Path("poc/data/ground_truth").glob("*.json"))
    if labels[gt.stem].get("price_deviation")
    and (
        po := PurchaseOrder.model_validate_json(gt.read_text(encoding="utf-8"))
    ).total_amount
    < s.auto_post_limit
][:3]
deps = Deps(
    erp=ErpClient(s.erp_base_url),
    tolerance=make_tolerance_lookup(s, make_store(s)),
    match_material=make_material_matcher(s),
    anomaly=anomaly_score,
    explain=make_explainer(s),
    auto_post_limit=s.auto_post_limit,
)

with postgres_checkpointer(s.events_database_url) as saver:
    graph = build_graph(deps, saver)
    for action, po in zip(["approve", "reject", "edit"], deviating):
        key = uuid.uuid4().hex
        state = run_or_resume(
            graph,
            AgentState(
                idempotency_key=key,
                correlation_id=key,
                source_event_id="demo",
                purchase_order=po,
            ),
        )
        print(f"\n=== {po.po_number}: {state.status}")
        print("Explanation:", state.explanation)

        if action == "edit":  # the clerk corrects the wrong price to the contract price
            wrong = {
                i.line_no for i in state.issues if i.code == IssueCode.PRICE_DEVIATION
            }
            agreed = {c.line_no: c.agreed_price for c in state.lines}
            lines = [
                line.model_copy(
                    update={
                        "unit_price": agreed[line.line_no],
                        "line_total": (line.quantity * agreed[line.line_no]).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                    }
                )
                if line.line_no in wrong
                else line
                for line in po.lines
            ]
            fixed = po.model_copy(
                update={
                    "lines": lines,
                    "total_amount": sum(l.line_total for l in lines),
                }
            )
            review = Review(
                action="edit",
                reviewer="clerk@alpentech.example",
                comment="Price corrected to contract price",
                edited_po=fixed,
            )
        else:
            review = Review(
                action=action,
                reviewer="clerk@alpentech.example",
                comment=f"Demo: {action}",
            )

        final = resume_with_review(graph, key, review)
        print(
            f"After '{action}': status={final.status} order={final.sales_order_number} "
            f"issues={[i.code.value for i in final.issues]}"
        )

        try:
            resume_with_review(graph, key, review)
        except ValueError as exc:
            print("Second decision refused:", exc)
