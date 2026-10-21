"""The agent graph for evaluations: real checks, an ERP that is only read.

Used by ml/judge_explanations.py and ml/eval_redteam.py. Needs the local stack (make up):
the ERP mock API, LiteLLM, the contract index (price tolerances) and MLflow (quantity
anomaly model; without it the anomaly score is 0.0). Orders are "created" in memory
(DryRunErp, as in local/agent_graph_eval.py); the checkpointer is an InMemorySaver, so a
run pauses in human_review with status awaiting_review.
"""

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from agent.erp import ErpClient
from agent.state import AgentState
from o2c_common.schema import PurchaseOrder

CENT = Decimal("0.01")
KEY_LENGTH = 16


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


def make_graph(explain: Callable[[AgentState], str] | None = None, settings=None):
    """The real graph with DryRunErp and an InMemorySaver. `explain` defaults to the real
    explainer (o2c-extract through LiteLLM)."""
    from agent.anomaly import anomaly_score
    from agent.contracts import make_store, make_tolerance_lookup
    from agent.explain import make_explainer
    from agent.graph import Deps, build_graph
    from agent.materials import make_material_matcher
    from agent.settings import Settings
    from langgraph.checkpoint.memory import InMemorySaver

    s = settings or Settings()
    deps = Deps(
        erp=DryRunErp(s.erp_base_url),
        tolerance=make_tolerance_lookup(s, make_store(s)),
        match_material=make_material_matcher(s),
        anomaly=anomaly_score,
        explain=explain or make_explainer(s),
        auto_post_limit=s.auto_post_limit,
    )
    return build_graph(deps, InMemorySaver())


def agent_key(name: str) -> str:
    """Idempotency key / thread id for an evaluation run (at least 16 characters)."""
    return name.ljust(KEY_LENGTH, "0")


def initial_state(po: PurchaseOrder, name: str) -> AgentState:
    key = agent_key(name)
    return AgentState(
        idempotency_key=key,
        correlation_id=key,
        source_event_id="eval",
        purchase_order=po,
    )


def run_agent(graph, po: PurchaseOrder, name: str) -> AgentState:
    """Run the graph on one PO until it posts or pauses for review."""
    from agent.graph import run

    state = initial_state(po, name)
    return run(graph, state, {"configurable": {"thread_id": state.idempotency_key}})
