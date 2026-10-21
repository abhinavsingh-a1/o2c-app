from datetime import date

import list_models
import pytest
from list_models import (
    is_safe,
    is_small_chat_model,
    main,
    pick_candidates,
    small_chat_models,
)

ACCOUNT = [
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4o-mini",
    "gpt-4o-mini-tts",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-search-preview",
    "gpt-5-mini",
    "gpt-5.1-codex-mini",
    "gpt-5.4-mini",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-luna",
    "gpt-6-sol",
    "gpt-audio-mini",
    "gpt-image-1-mini",
    "gpt-realtime-2.1-mini",
    "o4-mini",
    "text-embedding-3-small",
]


@pytest.mark.parametrize(
    "model_id",
    ["gpt-4.1-mini", "gpt-5.4-mini", "gpt-6-luna", "gpt-5.6-terra", "o4-mini"],
)
def test_small_chat_models(model_id):
    assert is_small_chat_model(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-4.1",
        "gpt-6-sol",
        "gpt-4.1-mini-2025-04-14",  # snapshot
        "gpt-4o-mini-tts",
        "gpt-4o-mini-transcribe",
        "gpt-4o-mini-search-preview",
        "gpt-5.1-codex-mini",
        "gpt-audio-mini",
        "gpt-image-1-mini",
        "gpt-realtime-2.1-mini",
        "text-embedding-3-small",
    ],
)
def test_not_small_chat_models(model_id):
    assert not is_small_chat_model(model_id)


def test_small_chat_models_are_sorted():
    assert small_chat_models(ACCOUNT) == [
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o-mini",
        "gpt-5-mini",
        "gpt-5.4-mini",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-6-luna",
        "o4-mini",
    ]


def test_is_safe():
    assert not is_safe("gpt-4.1-mini")  # 2027-01-14
    assert not is_safe("gpt-5.4-nano")  # 2027-04-01
    assert is_safe("gpt-6-luna")  # not scheduled
    assert is_safe("gpt-5.4-nano", until=date(2027, 4, 1))


def test_pick_candidates_prefers_safe_available_models():
    assert pick_candidates(ACCOUNT) == ["gpt-6-luna", "gpt-5.6-terra"]
    assert pick_candidates(ACCOUNT, count=1) == ["gpt-6-luna"]
    assert pick_candidates(["gpt-4.1-mini", "gpt-5.6-terra"]) == ["gpt-5.6-terra"]


def test_pick_candidates_skips_scheduled_models(monkeypatch):
    monkeypatch.setitem(list_models.SHUTDOWNS, "gpt-6-luna", date(2027, 3, 1))

    assert pick_candidates(ACCOUNT) == ["gpt-5.6-terra"]


def test_main_prints_models_and_candidates(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(list_models, "list_model_ids", lambda key: ACCOUNT)

    assert main() == ["gpt-6-luna", "gpt-5.6-terra"]
    out = capsys.readouterr().out
    assert "gpt-4.1-mini     shuts down 2027-01-14 (current)" in out
    assert "gpt-6-luna       no shutdown scheduled" in out
    assert "gpt-6-luna: named replacement" in out


def test_main_without_key_exits(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here

    with pytest.raises(SystemExit):
        main()
