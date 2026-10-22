import sys
from pathlib import Path

import agent.reviews
import analytics_sink.db

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ci_create_schemas

URL = "postgresql+psycopg://ci:ci@localhost:5432/ci"


def test_create_uses_the_services_own_schema_code(monkeypatch):
    calls = []
    monkeypatch.setattr(
        analytics_sink.db,
        "setup",
        lambda engine, bi_password: calls.append(
            ("setup", str(engine.url), bi_password)
        ),
    )
    monkeypatch.setattr(
        agent.reviews, "ReviewStore", lambda url: calls.append(("reviews", url))
    )

    ci_create_schemas.create(URL)

    assert calls == [
        ("setup", "postgresql+psycopg://ci:***@localhost:5432/ci", None),
        ("reviews", URL),
    ]


def test_main_needs_the_url(monkeypatch, capsys):
    monkeypatch.setattr(ci_create_schemas, "create", lambda url: None)

    assert ci_create_schemas.main([URL]) == 0
    assert "created raw, analytics" in capsys.readouterr().out
