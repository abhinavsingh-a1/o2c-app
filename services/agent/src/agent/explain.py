import json
from collections.abc import Callable

from langchain_openai import ChatOpenAI

from agent.settings import Settings
from agent.state import AgentState


def explanation_facts(state: AgentState) -> dict:
    """The order data and issues the explainer sees (and an LLM judge checks against)."""
    po = state.purchase_order
    return {
        "po_number": po.po_number,
        "customer": po.customer_name,
        "total": str(po.total_amount),
        "currency": po.currency,
        "lines": [
            {
                "line_no": line.line_no,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
            }
            for line in po.lines
        ],
        "issues": [issue.message for issue in state.issues],
    }


def make_explainer(
    settings: Settings,
    trace_id_for: Callable[[AgentState], str | None] | None = None,
):
    """`trace_id_for` lets an evaluation choose the Langfuse trace id of each call (to
    attach judge scores to it); services leave it to LiteLLM."""
    system_prompt = (settings.prompts_dir / "explain_exception.md").read_text(
        encoding="utf-8"
    )

    def explain(state: AgentState) -> str:
        metadata = {
            "trace_name": "explain_exception",
            "session_id": state.correlation_id,
            "tags": ["agent"],
        }
        trace_id = trace_id_for(state) if trace_id_for else None
        if trace_id:
            metadata["trace_id"] = trace_id
        llm = ChatOpenAI(
            model="o2c-extract",
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_master_key,
            temperature=0,
            extra_body={"metadata": metadata},
        )
        return llm.invoke(
            [
                ("system", system_prompt),
                ("human", json.dumps(explanation_facts(state), ensure_ascii=False)),
            ]
        ).content

    return explain
