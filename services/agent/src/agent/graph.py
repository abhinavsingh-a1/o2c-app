from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from langgraph.graph import END, START, StateGraph

from agent.contracts import Tolerance
from agent.erp import ErpClient, ErpRejected
from agent.materials import MaterialMatch
from agent.state import AgentState, Issue, IssueCode, LineCheck

CENT = Decimal("0.01")
ANOMALY_THRESHOLD = 0.8


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)  # commercial rounding


@dataclass
class Deps:
    erp: ErpClient
    tolerance: Callable[[str], Tolerance]
    match_material: Callable[[str, list[dict]], MaterialMatch]
    anomaly: Callable[[str, str, float], float]
    auto_post_limit: Decimal


def build_graph(deps: Deps, checkpointer=None):
    erp = deps.erp

    def resolve_customer(state: AgentState) -> dict:
        po = state.purchase_order
        if po.customer_number and erp.get_customer(po.customer_number):
            return {"customer_number": po.customer_number}
        matches = [
            c
            for c in erp.search_customers(po.customer_name)
            if _norm(c["name1"]) == _norm(po.customer_name)
        ]
        if len(matches) == 1:
            return {"customer_number": matches[0]["kunnr"]}
        return {
            "issues": [
                *state.issues,
                Issue(
                    code=IssueCode.UNKNOWN_CUSTOMER,
                    message=f"Customer '{po.customer_name}' was not found in the ERP.",
                ),
            ]
        }

    def resolve_materials(state: AgentState) -> dict:
        issues, lines, catalogue = list(state.issues), [], None
        for line in state.purchase_order.lines:
            if line.material_number and erp.get_material(line.material_number):
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=line.material_number,
                        material_source="document",
                    )
                )
                continue
            hits = erp.search_materials(line.description)
            if len(hits) == 1:
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=hits[0]["matnr"],
                        material_source="erp_search",
                    )
                )
                continue
            catalogue = catalogue or erp.search_materials("")
            match = deps.match_material(line.description, catalogue)
            if match.matnr:
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=match.matnr,
                        material_source="llm",
                    )
                )
            else:
                lines.append(LineCheck(line_no=line.line_no))
                issues.append(
                    Issue(
                        code=IssueCode.UNKNOWN_MATERIAL,
                        line_no=line.line_no,
                        message=f"Line {line.line_no}: no material found for "
                        f"'{line.description}'.",
                    )
                )
        return {"lines": lines, "issues": issues}

    def check_prices(state: AgentState) -> dict:
        po = state.purchase_order
        issues = list(state.issues)
        for line in po.lines:
            if _money(line.quantity * line.unit_price) != line.line_total:
                issues.append(
                    Issue(
                        code=IssueCode.TOTAL_MISMATCH,
                        line_no=line.line_no,
                        message=f"Line {line.line_no}: {line.quantity} x {line.unit_price} "
                        f"is not {line.line_total}.",
                    )
                )
        if sum((line.line_total for line in po.lines), Decimal(0)) != po.total_amount:
            issues.append(
                Issue(
                    code=IssueCode.TOTAL_MISMATCH,
                    message=f"The line totals do not add up to {po.total_amount}.",
                )
            )
        if state.customer_number is None:
            return {"issues": issues}

        tolerance = deps.tolerance(state.customer_number).tolerance_pct or Decimal(0)
        po_lines = {line.line_no: line for line in po.lines}
        checked = []
        for check in state.lines:
            line = po_lines[check.line_no]
            if check.material_number is None:
                checked.append(check)
                continue
            price = erp.get_price(
                check.material_number, state.customer_number, po.po_date
            )
            if price is None:
                issues.append(
                    Issue(
                        code=IssueCode.NO_AGREED_PRICE,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: no valid price for material "
                        f"{check.material_number} on {po.po_date}.",
                    )
                )
                checked.append(check)
                continue
            agreed = Decimal(str(price["kbetr"]))
            deviation = _money(abs(line.unit_price - agreed) / agreed * 100)
            if deviation > tolerance:
                issues.append(
                    Issue(
                        code=IssueCode.PRICE_DEVIATION,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: price {line.unit_price} vs agreed "
                        f"{agreed} ({deviation}% > tolerance {tolerance}%).",
                    )
                )
            checked.append(
                check.model_copy(
                    update={
                        "agreed_price": agreed,
                        "tolerance_pct": tolerance,
                        "deviation_pct": deviation,
                    }
                )
            )
        return {"lines": checked, "issues": issues}

    def check_anomalies(state: AgentState) -> dict:
        if state.customer_number is None:
            return {}
        issues, checked = list(state.issues), []
        quantities = {
            line.line_no: line.quantity for line in state.purchase_order.lines
        }
        for check in state.lines:
            if check.material_number is None:
                checked.append(check)
                continue
            score = deps.anomaly(
                state.customer_number,
                check.material_number,
                float(quantities[check.line_no]),
            )
            if score > ANOMALY_THRESHOLD:
                issues.append(
                    Issue(
                        code=IssueCode.QUANTITY_ANOMALY,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: unusual quantity "
                        f"{quantities[check.line_no]} (score {score:.2f}).",
                    )
                )
            checked.append(check.model_copy(update={"anomaly_score": score}))
        return {"lines": checked, "issues": issues}

    def decide(state: AgentState) -> dict:
        issues = list(state.issues)
        total = state.purchase_order.total_amount
        if total > deps.auto_post_limit:
            issues.append(
                Issue(
                    code=IssueCode.ABOVE_AUTO_POST_LIMIT,
                    message=f"Order total {total} is above the auto-post limit "
                    f"{deps.auto_post_limit}.",
                )
            )
        return {"issues": issues, "decision": "exception" if issues else "auto_post"}

    def post_order(state: AgentState) -> dict:
        po = state.purchase_order
        materials = {check.line_no: check.material_number for check in state.lines}
        order = {
            "kunnr": state.customer_number,
            "bstnk": po.po_number,
            "bstdk": po.po_date.isoformat(),
            "waerk": po.currency,
            "idempotency_key": state.idempotency_key,
            "items": [
                {
                    "matnr": materials[line.line_no],
                    "arktx": line.description[:120],
                    "kwmeng": str(line.quantity),
                    "vrkme": line.unit,
                    "netpr": str(line.unit_price),
                }
                for line in po.lines
            ],
        }
        try:
            created = erp.create_order(order)
        except ErpRejected as exc:
            return {
                "issues": [
                    *state.issues,
                    Issue(
                        code=IssueCode.ORDER_VERIFY_FAILED,
                        message=f"The ERP rejected the order: {exc}",
                    ),
                ]
            }
        return {"sales_order_number": created["vbeln"]}

    def verify_order(state: AgentState) -> dict:
        order = erp.get_order(state.sales_order_number)
        po = state.purchase_order
        if (
            order
            and order["status"] == "OPEN"
            and Decimal(str(order["netwr"])) == po.total_amount
            and len(order["items"]) == len(po.lines)
        ):
            return {"status": "posted"}
        return {
            "issues": [
                *state.issues,
                Issue(
                    code=IssueCode.ORDER_VERIFY_FAILED,
                    message=f"Sales order {state.sales_order_number} in the ERP does not match the PO.",
                ),
            ]
        }

    def cancel_order(state: AgentState) -> dict:
        erp.cancel_order(state.sales_order_number)  # saga compensation
        return {}

    def explain_exception(state: AgentState) -> dict:
        # Step 5 replaces this with a GPT-written explanation and a human-review pause.
        text = "\n".join(f"- {issue.message}" for issue in state.issues)
        return {"explanation": text, "status": "awaiting_review"}

    def after_decide(state: AgentState) -> str:
        return "post_order" if state.decision == "auto_post" else "explain_exception"

    def after_post(state: AgentState) -> str:
        return "verify_order" if state.sales_order_number else "explain_exception"

    def after_verify(state: AgentState) -> str:
        return END if state.status == "posted" else "cancel_order"

    graph = StateGraph(AgentState)
    for name, node in [
        ("resolve_customer", resolve_customer),
        ("resolve_materials", resolve_materials),
        ("check_prices", check_prices),
        ("check_anomalies", check_anomalies),
        ("decide", decide),
        ("post_order", post_order),
        ("verify_order", verify_order),
        ("cancel_order", cancel_order),
        ("explain_exception", explain_exception),
    ]:
        graph.add_node(name, node)

    graph.add_edge(START, "resolve_customer")
    graph.add_edge("resolve_customer", "resolve_materials")
    graph.add_edge("resolve_materials", "check_prices")
    graph.add_edge("check_prices", "check_anomalies")
    graph.add_edge("check_anomalies", "decide")
    graph.add_conditional_edges(
        "decide",
        after_decide,
        {"post_order": "post_order", "explain_exception": "explain_exception"},
    )
    graph.add_conditional_edges(
        "post_order",
        after_post,
        {"verify_order": "verify_order", "explain_exception": "explain_exception"},
    )
    graph.add_conditional_edges(
        "verify_order", after_verify, {END: END, "cancel_order": "cancel_order"}
    )
    graph.add_edge("cancel_order", "explain_exception")
    graph.add_edge("explain_exception", END)
    return graph.compile(checkpointer=checkpointer)


def run(graph, state: AgentState, config: dict | None = None) -> AgentState:
    return AgentState.model_validate(graph.invoke(state, config))
