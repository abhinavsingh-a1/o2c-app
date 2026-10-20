import json
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from agent.settings import Settings


class MaterialMatch(BaseModel):
    matnr: str | None
    reason: str


class MaterialMatcher:
    def __init__(self, chain, prompts_dir: Path):
        self._chain = chain
        self._template = (prompts_dir / "match_material.md").read_text(encoding="utf-8")

    def __call__(self, description: str, candidates: list[dict]) -> MaterialMatch:
        listing = "\n".join(
            f"{c['matnr']}: {c['maktx']} ({c['meins']})" for c in candidates
        )
        prompt = self._template.replace("{candidates}", listing).replace(
            "{schema}", json.dumps(MaterialMatch.model_json_schema())
        )
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
    )
    return MaterialMatcher(
        llm.with_structured_output(MaterialMatch, method="json_mode"),
        settings.prompts_dir,
    )
