import json

from langchain_openai import ChatOpenAI

from agent.settings import Settings
from agent.state import AgentState


def make_explainer(settings: Settings):
    system_prompt = (settings.prompts_dir / "explain_exception.md").read_text(
        encoding="utf-8"
    )

    def explain(state: AgentState) -> str:
        po = state.purchase_order
        facts = {
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
        llm = ChatOpenAI(
            model="o2c-extract",
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_master_key,
            temperature=0,
            extra_body={
                "metadata": {
                    "trace_name": "explain_exception",
                    "session_id": state.correlation_id,
                    "tags": ["agent"],
                }
            },
        )
        return llm.invoke(
            [
                ("system", system_prompt),
                ("human", json.dumps(facts, ensure_ascii=False)),
            ]
        ).content

    return explain
