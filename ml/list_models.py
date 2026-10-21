"""List the OpenAI models of this account and pick extraction candidates (E11, ADR-008).

Usage: uv run python ml/list_models.py

Reads OPENAI_API_KEY from the environment or .env and calls the OpenAI models endpoint
directly: listing models is not an LLM call, and LiteLLM only knows the models in its
own config. Prints the small/mini general-purpose chat models available to the account,
their shutdown dates from the OpenAI deprecations page, and the chosen candidates.

SHUTDOWNS and TIERS are copied from https://developers.openai.com/api/docs/deprecations
and the model pages (checked 2026-10-21); update them when OpenAI announces new dates.
"""

import os
import re
from datetime import date

from dotenv import dotenv_values

CURRENT = "gpt-4.1-mini"  # behind the LiteLLM alias o2c-extract
SAFE_UNTIL = date(2027, 7, 1)  # a candidate must not be scheduled to shut down before

# Deprecations page: model (or snapshot) -> shutdown date.
SHUTDOWNS = {
    "gpt-4.1-mini": date(2027, 1, 14),
    "gpt-4.1-mini-2025-04-14": date(2027, 1, 14),
    "gpt-4o-mini": date(2027, 1, 14),
    "gpt-4o-mini-2024-07-18": date(2027, 1, 14),
    "gpt-4.1-nano": date(2026, 10, 23),
    "gpt-4.1-nano-2025-04-14": date(2026, 10, 23),
    "o3-mini": date(2026, 10, 23),
    "o3-mini-2025-01-31": date(2026, 10, 23),
    "o4-mini": date(2026, 10, 23),
    "o4-mini-2025-04-16": date(2026, 10, 23),
    "gpt-5-mini": date(2026, 12, 11),
    "gpt-5-mini-2025-08-07": date(2026, 12, 11),
    "gpt-5-nano": date(2026, 12, 11),
    "gpt-5-nano-2025-08-07": date(2026, 12, 11),
    "gpt-5.4-nano": date(2027, 4, 1),
    "gpt-5.4-nano-2026-03-17": date(2027, 4, 1),
}

# Small tiers without "mini"/"nano" in the name: luna (most efficient) and terra (the
# former mini tier). sol and astra are the large tiers.
SMALL_TIERS = ("luna", "terra")

# Ranked: the deprecations page names gpt-6-luna as the replacement of gpt-4.1-mini, and
# gpt-5.6-terra as the replacement of the mini tier (gpt-5-mini).
PREFERENCE = {
    "gpt-6-luna": "named replacement of gpt-4.1-mini on the deprecations page; "
    "$0.10/$0.50 per 1M tokens (gpt-4.1-mini: $0.40/$1.60)",
    "gpt-5.6-terra": "replacement of the mini tier (gpt-5-mini); stronger, "
    "but $2/$12 per 1M tokens",
}

NOT_CHAT = re.compile(
    r"audio|realtime|tts|transcribe|search|image|embedding|moderation|codex|"
    r"instruct|live|translate|whisper|sora|chat-latest|-pro\b"
)
SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def is_small_chat_model(model_id: str) -> bool:
    """A small/mini general-purpose GPT/o-series chat model (no snapshot, no audio,
    image, search or codex variant)."""
    if NOT_CHAT.search(model_id) or SNAPSHOT.search(model_id):
        return False
    if not re.match(r"(gpt-|o\d)", model_id):
        return False
    return bool(re.search(r"mini|nano", model_id)) or model_id.endswith(SMALL_TIERS)


def small_chat_models(model_ids: list[str]) -> list[str]:
    return sorted(m for m in model_ids if is_small_chat_model(m))


def is_safe(model_id: str, until: date = SAFE_UNTIL) -> bool:
    """Not on a shutdown schedule before `until`."""
    shutdown = SHUTDOWNS.get(model_id)
    return shutdown is None or shutdown >= until


def pick_candidates(model_ids: list[str], count: int = 2) -> list[str]:
    """The preferred safe models that this account can use, best first."""
    available = set(model_ids)
    return [m for m in PREFERENCE if m in available and is_safe(m)][:count]


def list_model_ids(api_key: str) -> list[str]:
    from openai import OpenAI

    return [model.id for model in OpenAI(api_key=api_key).models.list()]


def main() -> list[str]:
    api_key = os.environ.get("OPENAI_API_KEY") or dotenv_values(".env").get(
        "OPENAI_API_KEY"
    )
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (environment or .env)")
    model_ids = list_model_ids(api_key)

    print(f"{len(model_ids)} models; small/mini general-purpose chat models:")
    for model in small_chat_models(model_ids):
        shutdown = SHUTDOWNS.get(model)
        status = f"shuts down {shutdown}" if shutdown else "no shutdown scheduled"
        marker = " (current)" if model == CURRENT else ""
        print(f"  {model:<16} {status}{marker}")

    candidates = pick_candidates(model_ids)
    print(f"\ncandidates (no shutdown before {SAFE_UNTIL}):")
    for model in candidates:
        print(f"  {model}: {PREFERENCE[model]}")
    return candidates


if __name__ == "__main__":
    main()
