import json
import uuid
from pathlib import Path

from agent.anomaly import anomaly_score
from agent.checkpoint import postgres_checkpointer, run_or_resume, thread_config
from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.explain import make_explainer
from agent.graph import Deps, build_graph
from agent.materials import make_material_matcher
from agent.settings import Settings
from agent.state import AgentState
from o2c_common.schema import PurchaseOrder


class TestErp(ErpClient):
    """Real ERP client that counts create_order calls and can crash once at a chosen point."""

    def __init__(self, base_url: str, crash_at: str | None = None):
        super().__init__(base_url)
        self.crash_at, self.creates = crash_at, 0

    def create_order(self, order: dict) -> dict:
        self.creates += 1
        result = super().create_order(order)
        if self.crash_at == "after_create":
            self.crash_at = None
            raise RuntimeError(
                "simulated crash: ERP created the order, checkpoint not saved"
            )
        return result

    def get_order(self, vbeln: str) -> dict | None:
        if self.crash_at == "verify":
            self.crash_at = None
            raise RuntimeError("simulated crash while verifying the order")
        return super().get_order(vbeln)


s = Settings()
labels = json.loads(Path("poc/data/labels.json").read_text(encoding="utf-8"))
clean = next(  # a PO without a price deviation and below the auto-post limit
    po
    for gt in sorted(Path("poc/data/ground_truth").glob("*.json"))
    if not labels[gt.stem].get("price_deviation")
    and (
        po := PurchaseOrder.model_validate_json(gt.read_text(encoding="utf-8"))
    ).total_amount
    < s.auto_post_limit
)
store = make_store(s)
tolerance, matcher = make_tolerance_lookup(s, store), make_material_matcher(s)


def make_graph(erp: TestErp, saver):
    return build_graph(
        Deps(
            erp=erp,
            tolerance=tolerance,
            match_material=matcher,
            anomaly=anomaly_score,
            explain=make_explainer(s),
            auto_post_limit=s.auto_post_limit,
        ),
        saver,
    )


with postgres_checkpointer(s.events_database_url) as saver:
    for crash_at in ["after_create", "verify"]:
        key = uuid.uuid4().hex
        initial = AgentState(
            idempotency_key=key,
            correlation_id=key,
            source_event_id="crash-test",
            purchase_order=clean,
        )
        print(f"\n=== Scenario: crash {crash_at} | PO {clean.po_number} | key {key}")

        first = TestErp(s.erp_base_url, crash_at=crash_at)
        try:
            run_or_resume(make_graph(first, saver), initial)
        except RuntimeError as exc:
            print("1st run crashed:", exc)
        print(
            "nodes left after crash:",
            make_graph(first, saver).get_state(thread_config(key)).next,
        )

        second = TestErp(s.erp_base_url)
        result = run_or_resume(make_graph(second, saver), initial)
        print(
            f"2nd run: status={result.status} order={result.sales_order_number} "
            f"create_order calls={second.creates}"
        )

        third = TestErp(s.erp_base_url)
        again = run_or_resume(make_graph(third, saver), initial)
        print(
            f"3rd run (already finished): status={again.status} create_order calls={third.creates}"
        )
