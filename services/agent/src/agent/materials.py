from pathlib import Path

from langchain_openai import ChatOpenAI
from o2c_common.metrics import llm_http_client
from pydantic import BaseModel, ConfigDict

from agent.settings import Settings


class MaterialMatch(BaseModel):
    # Strict structured outputs: no extra keys, every field required (ADR-008).
    model_config = ConfigDict(extra="forbid")
    matnr: str | None
    reason: str


def structured_matcher(llm: ChatOpenAI):
    """The LLM with OpenAI strict structured outputs: the API enforces the schema, so the
    prompt only describes the fields (the raw schema in the prompt got echoed back wrapped
    in {"properties": ...} by gpt-6-luna)."""
    return llm.with_structured_output(MaterialMatch, method="json_schema", strict=True)


class MaterialMatcher:
    def __init__(self, chain, prompts_dir: Path):
        self._chain = chain
        self._template = (prompts_dir / "match_material.md").read_text(encoding="utf-8")

    def __call__(self, description: str, candidates: list[dict]) -> MaterialMatch:
        listing = "\n".join(
            f"{c['matnr']}: {c['maktx']} ({c['meins']})" for c in candidates
        )
        prompt = self._template.replace("{candidates}", listing)
        result = self._chain.invoke([("system", prompt), ("human", description)])
        valid = {c["matnr"] for c in candidates}
        if result.matnr is not None and result.matnr not in valid:
            # The model returned a number that does not exist: never trust it.
            return MaterialMatch(
                matnr=None, reason=f"rejected invented number {result.matnr}"
            )
        return result


def make_material_matcher(settings: Settings) -> MaterialMatcher:
    llm = ChatOpenAI(
        model="o2c-extract",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        temperature=0,
        extra_body={"metadata": {"trace_name": "match_material", "tags": ["agent"]}},
        http_client=llm_http_client("agent"),  # o2c_llm_cost_usd_total
    )
    return MaterialMatcher(structured_matcher(llm), settings.prompts_dir)
