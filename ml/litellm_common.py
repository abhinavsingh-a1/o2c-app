"""LiteLLM key and cost helpers for the evaluation scripts (ADR-008, ADR-010).

Evaluations call LiteLLM with the virtual key LITELLM_KEY_EVAL when it is set (environment
or .env; created by ml/litellm_keys.py), so their spend is tracked and capped apart from
the services. The scripts build the services' own settings, which read the key from
LITELLM_MASTER_KEY (the field name is kept for now), so use_eval_key() puts the eval key
there for the current process. Without LITELLM_KEY_EVAL, the master key is used as before.

alias_target() reads which model an alias points at in local/litellm/config.yaml, so a
gate result can be tied to the model it measured (ADR-008 migration procedure).
"""

import os
import re
from contextlib import contextmanager
from pathlib import Path

import httpx
import yaml
from langfuse_common import setting

EVAL_KEY = "LITELLM_KEY_EVAL"
MASTER_KEY = "LITELLM_MASTER_KEY"
COST_HEADER = "x-litellm-response-cost"
FALLBACK_HEADER = "x-litellm-attempted-fallbacks"
PLACEHOLDER = "replace-me"  # .env.example values are not keys
LITELLM_CONFIG = (
    Path(__file__).resolve().parents[1] / "local" / "litellm" / "config.yaml"
)
# litellm_params that do not change which model answers or how
CONNECTION_PARAMS = {"api_key", "api_base", "api_version"}


def eval_key() -> str | None:
    """LITELLM_KEY_EVAL from the environment or .env; None if unset or a placeholder."""
    key = setting(EVAL_KEY)
    return key if key and PLACEHOLDER not in key else None


def use_eval_key() -> bool:
    """Make this process call LiteLLM with the eval key (if one is set)."""
    key = eval_key()
    if key:
        os.environ[MASTER_KEY] = key
    return key is not None


def alias_target(alias: str, config: Path = LITELLM_CONFIG) -> str:
    """The model behind a LiteLLM alias with its model parameters, e.g.
    "openai/gpt-6-luna (reasoning_effort=low)". ValueError if the alias is not defined
    exactly once. The running LiteLLM uses this file after a restart."""
    entries = [
        m
        for m in yaml.safe_load(config.read_text(encoding="utf-8"))["model_list"]
        if m["model_name"] == alias
    ]
    if len(entries) != 1:
        raise ValueError(f"{alias!r} is defined {len(entries)} times in {config.name}")
    params = {
        k: v
        for k, v in entries[0]["litellm_params"].items()
        if k not in CONNECTION_PARAMS
    }
    model = params.pop("model")
    extras = ", ".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{model} ({extras})" if extras else model


def target_slug(target: str) -> str:
    """A file name part for alias_target(): "openai-gpt-6-luna-reasoning_effort-low"."""
    return re.sub(r"[^a-z0-9._]+", "-", target.lower()).strip("-")


class CostMeter:
    """Sums LiteLLM's x-litellm-response-cost header over every HTTP call made while it
    is active, so a whole run's OpenAI cost is known without threading a recorder
    through every client. It wraps Client.send of httpx and of httpx2 (the OpenAI SDK
    3.x and LangChain's ChatOpenAI use httpx2; evaluation recorders use httpx).
    It also counts calls that LiteLLM answered through a fallback model."""

    def __init__(self):
        self.calls = 0
        self.cost_usd = 0.0
        self.fallbacks = 0

    def record(self, response) -> None:
        cost = response.headers.get(COST_HEADER)
        if cost is not None:
            self.calls += 1
            self.cost_usd += float(cost)
        if int(response.headers.get(FALLBACK_HEADER) or 0) > 0:
            self.fallbacks += 1

    @contextmanager
    def active(self):
        originals = {cls: cls.send for cls in client_classes()}

        def wrap(original):
            def send(client, request, *args, **kwargs):
                response = original(client, request, *args, **kwargs)
                self.record(response)
                return response

            return send

        for cls, original in originals.items():
            cls.send = wrap(original)
        try:
            yield self
        finally:
            for cls, original in originals.items():
                cls.send = original


def client_classes() -> list[type]:
    """The synchronous HTTP client classes LLM calls can go through."""
    classes = [httpx.Client]
    try:
        import httpx2

        classes.append(httpx2.Client)
    except ImportError:
        pass
    return classes


def admin_client(timeout: float = 30.0) -> httpx.Client:
    """httpx client for LiteLLM's admin API (key management, spend) with the master key."""
    key = setting(MASTER_KEY)
    if not key:
        raise SystemExit(f"{MASTER_KEY} is not set")
    return httpx.Client(
        base_url=setting("LITELLM_BASE_URL", "http://localhost:4000"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
