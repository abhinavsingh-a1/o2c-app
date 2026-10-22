"""Regression (ADR-008): gpt-6-luna answered the prompt-embedded JSON schema with
{"properties": {...}}. The real ChatOpenAI parsers, fed recorded chat completions, must
accept a correct answer and reject a wrapped one, never unwrap it."""

import json
from decimal import Decimal

import httpx
import pytest
from agent.contracts import Tolerance, structured_tolerance
from agent.materials import MaterialMatch, structured_matcher
from langchain_core.exceptions import OutputParserException
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

MESSAGES = [("system", "prompt"), ("human", "input")]


def recorded_llm(content: str, requests: list | None = None) -> ChatOpenAI:
    """ChatOpenAI whose LiteLLM answers with one recorded chat completion."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-recorded",
                "object": "chat.completion",
                "created": 0,
                "model": "o2c-extract",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    return ChatOpenAI(
        model="o2c-extract",
        api_key="test",
        base_url="http://litellm",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


TOLERANCE = {"tolerance_pct": 2, "evidence": "deviations up to 2% are accepted"}
MATCH = {"matnr": "400103", "reason": "same V-belt profile and length"}


def test_tolerance_parser_accepts_a_correct_answer():
    chain = structured_tolerance(recorded_llm(json.dumps(TOLERANCE)))

    assert chain.invoke(MESSAGES) == Tolerance(
        tolerance_pct=Decimal(2), evidence="deviations up to 2% are accepted"
    )


def test_tolerance_parser_rejects_a_wrapped_answer():
    chain = structured_tolerance(recorded_llm(json.dumps({"properties": TOLERANCE})))

    with pytest.raises(OutputParserException):
        chain.invoke(MESSAGES)


def test_tolerance_parser_rejects_extra_keys():
    chain = structured_tolerance(
        recorded_llm(json.dumps({**TOLERANCE, "properties": {}}))
    )

    with pytest.raises(OutputParserException):
        chain.invoke(MESSAGES)


def test_tolerance_uses_json_mode():
    requests: list[dict] = []
    structured_tolerance(recorded_llm(json.dumps(TOLERANCE), requests)).invoke(MESSAGES)

    assert requests[0]["response_format"] == {"type": "json_object"}


def test_material_parser_accepts_a_correct_answer():
    chain = structured_matcher(recorded_llm(json.dumps(MATCH)))

    assert chain.invoke(MESSAGES) == MaterialMatch(**MATCH)


def test_material_parser_rejects_a_wrapped_answer():
    chain = structured_matcher(recorded_llm(json.dumps({"properties": MATCH})))

    with pytest.raises(ValidationError):
        chain.invoke(MESSAGES)


def test_material_matching_uses_strict_structured_outputs():
    requests: list[dict] = []
    structured_matcher(recorded_llm(json.dumps(MATCH), requests)).invoke(MESSAGES)

    fmt = requests[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert fmt["json_schema"]["schema"]["required"] == ["matnr", "reason"]
