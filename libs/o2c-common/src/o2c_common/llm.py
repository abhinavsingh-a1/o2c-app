"""OpenAI structured outputs for calls made with the OpenAI client directly (ADR-008).

LangChain callers use llm.with_structured_output(Model, method="json_schema", strict=True);
this builds the same response_format for client.chat.completions.create(...). With strict
structured outputs the API sends the schema itself, so the prompt describes the fields in
words instead of pasting the raw JSON schema (which some models echoed back wrapped in
{"properties": {...}}).
"""

from pydantic import BaseModel


def _check_strict(schema: dict, where: str) -> None:
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{where}: needs extra='forbid' for strict mode")
        missing = set(schema.get("properties", {})) - set(schema.get("required", []))
        if missing:
            raise ValueError(
                f"{where}: optional fields {sorted(missing)} (strict mode)"
            )
    for name, sub in schema.get("$defs", {}).items():
        _check_strict(sub, name)


def strict_response_format(model: type[BaseModel]) -> dict:
    """response_format for OpenAI strict structured outputs from a pydantic model.

    ValueError if the model cannot be used in strict mode: every object needs
    extra="forbid" (additionalProperties false) and every field must be required
    (use `X | None` without a default for optional values).
    """
    schema = model.model_json_schema()
    _check_strict(schema, model.__name__)
    return {
        "type": "json_schema",
        "json_schema": {"name": model.__name__, "strict": True, "schema": schema},
    }
