import re

import pytest
from o2c_common.llm import strict_response_format
from pydantic import BaseModel, ConfigDict


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None
    inner: Inner


def test_strict_response_format():
    fmt = strict_response_format(Answer)

    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Answer"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == Answer.model_json_schema()


class Open(BaseModel):
    value: str


class OpenInner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: Open


class WithDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "x"


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (Open, "extra='forbid'"),
        (OpenInner, "Open: needs extra='forbid'"),
        (WithDefault, "optional fields ['value']"),
    ],
)
def test_models_that_strict_mode_rejects(model, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        strict_response_format(model)
