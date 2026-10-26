import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from analytics_sink import app, db
from analytics_sink.db import (
    dlq,
    dlq_row,
    event_row,
    events,
    insert_ignoring_duplicates,
    llm_spend,
    setup,
    setup_statements,
    upsert_spend,
    usable_password,
)
from analytics_sink.spend import (
    EPOCH,
    OVERLAP,
    SpendCopier,
    run_periodically,
    service_tag,
    spend_row,
)
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, InMemoryStore, RawMessageConsumer
from o2c_common.metrics import sample
from sqlalchemy.dialects import postgresql

KEY = "k" * 64


class FakeStore:
    """raw.* in memory, keyed like the real primary keys (duplicates are ignored)."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.dlq: dict[tuple, dict] = {}
        self.spend: dict[str, dict] = {}

    def insert_event(self, row):
        self.events.setdefault(row["event_id"], row)

    def insert_dlq(self, row):
        key = (row["topic"], row["kafka_partition"], row["kafka_offset"])
        self.dlq.setdefault(key, row)

    def insert_spend(self, rows):  # as upsert_spend
        for row in rows:
            old = self.spend.get(row["request_id"])
            if old is None:
                self.spend[row["request_id"]] = row
            elif old["service"] is None:
                old.update(session_id=row["session_id"], service=row["service"])

    def max_spend_created_at(self):
        return max(
            (
                r["source_created_at"]
                for r in self.spend.values()
                if r["service"] is not None
            ),
            default=None,
        )


class FakeMessage:
    def __init__(self, value, topic="orders.dlq", partition=0, offset=7, headers=None):
        self._value, self._topic = value, topic
        self._partition, self._offset = partition, offset
        self._headers = headers

    def value(self):
        return self._value

    def key(self):
        return KEY.encode()

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def headers(self):
        return self._headers

    def timestamp(self):
        return (1, 1_792_000_000_000)  # TIMESTAMP_CREATE_TIME, ms


class FakeProducer:
    def send_raw(self, *args, **kwargs):
        pass

    def flush(self, timeout=10.0):
        pass


def received() -> Envelope:
    return Envelope.new("docs.received", "intake", KEY, {"filename": "PO-1.pdf"})


# --- events ---


def test_event_row_keeps_the_whole_envelope():
    parent = received()
    child = parent.child("docs.classified", "classifier", {"doc_type": "invoice"})

    row = event_row(child)

    assert row["event_id"] == str(child.event_id)
    assert row["topic"] == row["event_type"] == "docs.classified"
    assert row["causation_id"] == str(parent.event_id)
    assert row["correlation_id"] == KEY
    assert row["idempotency_key"] == KEY
    assert row["producer"] == "classifier"
    assert row["payload"] == {"doc_type": "invoice"}
    assert row["occurred_at"] == child.occurred_at
    assert event_row(parent)["causation_id"] is None


def test_a_redelivered_event_is_stored_once():
    store = FakeStore()
    consumer = EventConsumer(
        bootstrap="unused:9092",
        group_id=app.GROUP,
        topics=app.TOPICS,
        handler=app.make_event_handler(store),
        producer=FakeProducer(),
        store=InMemoryStore(),
        base_delay=0,
    )
    msg = FakeMessage(received().to_bytes(), topic="docs.received")

    consumer.process(msg)
    consumer.process(msg)  # redelivered after a crash before the commit

    assert len(store.events) == 1


def test_the_handler_alone_is_idempotent_too():
    store, event = FakeStore(), received()
    handle = app.make_event_handler(store)

    handle(event)
    handle(event)  # e.g. the processed-events mark was lost

    assert list(store.events) == [str(event.event_id)]


def test_the_sink_reads_every_pipeline_topic():
    assert app.TOPICS == [
        "docs.received",
        "docs.classified",
        "orders.extracted",
        "orders.validated",
        "orders.posted",
        "orders.exceptions",
    ]
    assert app.GROUP == "analytics-sink"


# --- DLQ ---


def test_dlq_row_keeps_raw_value_and_headers():
    msg = FakeMessage(
        b"\xffnot json",
        partition=2,
        offset=42,
        headers=[("error", b"invalid envelope"), ("original_topic", b"docs.received")],
    )

    row = dlq_row(msg)

    assert (row["topic"], row["kafka_partition"], row["kafka_offset"]) == (
        "orders.dlq",
        2,
        42,
    )
    assert row["value"] == b"\xffnot json"  # as received, even if not UTF-8
    assert row["headers"] == {
        "error": "invalid envelope",
        "original_topic": "docs.received",
    }
    assert row["kafka_key"] == KEY
    assert row["kafka_timestamp"] == datetime.fromtimestamp(1_792_000_000, UTC)


def test_dlq_messages_are_copied_once_through_the_raw_consumer():
    store = FakeStore()
    consumer = RawMessageConsumer(
        bootstrap="unused:9092",
        group_id=app.DLQ_GROUP,
        topics=["orders.dlq"],
        handler=app.make_dlq_handler(store),
        base_delay=0,
    )
    msg = FakeMessage(b"garbage", headers=None)

    consumer.process(msg)
    consumer.process(msg)

    assert list(store.dlq) == [("orders.dlq", 0, 7)]
    assert store.dlq[("orders.dlq", 0, 7)]["headers"] == {}


# --- idempotent SQL ---


@pytest.mark.parametrize(
    ("table", "key"),
    [
        (events, "(event_id)"),
        (dlq, "(topic, kafka_partition, kafka_offset)"),
    ],
)
def test_every_insert_ignores_duplicates_on_the_primary_key(table, key):
    sql = str(insert_ignoring_duplicates(table).compile(dialect=postgresql.dialect()))

    assert f"ON CONFLICT {key} DO NOTHING" in sql
    assert sql.startswith(f"INSERT INTO raw.{table.name}")


# --- LiteLLM spend ---


def litellm_row(n: int, created: datetime, alias="extractor") -> dict:
    return {
        "request_id": f"req-{n}",
        "start_time": created - timedelta(seconds=2),
        "spend": 0.001,
        "model": "openai/gpt-6-luna",
        "model_group": "o2c-extract",
        "key_alias": alias,
        "session_id": "k" * 64,
        "request_tags": ["extractor", "User-Agent: OpenAI"],
        "created_at": created,
    }


class FakeLiteLLM:
    """LiteLLM_SpendLogs: rows ordered by created_at; records the queries."""

    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def __call__(self, since, limit, offset):
        self.queries.append((since, limit, offset))
        matching = [r for r in self.rows if r["created_at"] >= since]
        return matching[offset : offset + limit]


T0 = datetime(2026, 10, 22, 9, 0, 0)  # naive UTC, as LiteLLM stores it


def test_spend_row_converts_litellm_columns():
    row = spend_row(litellm_row(1, T0, alias=None))

    assert row["start_time"] == (T0 - timedelta(seconds=2)).replace(tzinfo=UTC)
    assert row["spend_usd"] == 0.001
    assert row["key_alias"] is None  # the master key
    assert row["model_group"] == "o2c-extract"
    assert row["source_created_at"] == T0
    assert row["session_id"] == "k" * 64
    assert row["service"] == "extractor"


@pytest.mark.parametrize(
    ("tags", "service"),
    [
        (["agent", "User-Agent: OpenAI"], "agent"),
        (["User-Agent: OpenAI", "eval"], "eval"),
        (["User-Agent: curl"], "untagged"),
        ([], "untagged"),
        (None, "untagged"),
    ],
)
def test_service_tag(tags, service):
    assert service_tag(tags) == service


def test_spend_upsert_only_completes_rows_copied_before_the_new_columns():
    sql = str(upsert_spend().compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT (request_id) DO UPDATE" in sql
    assert "SET session_id = excluded.session_id, service = excluded.service" in sql
    assert "WHERE raw.llm_spend.service IS NULL" in sql


def test_rows_without_service_are_read_again_once_and_completed():
    store = FakeStore()
    old = spend_row(litellm_row(1, T0)) | {"session_id": None, "service": None}
    store.spend["req-1"] = old  # copied by the first version of the sink
    litellm = FakeLiteLLM([litellm_row(1, T0), litellm_row(2, T0)])

    SpendCopier(litellm, store).run_once()

    assert litellm.queries[0][0] == EPOCH  # no complete row yet: read everything
    assert store.spend["req-1"]["service"] == "extractor"
    assert store.spend["req-1"]["session_id"] == "k" * 64
    assert store.max_spend_created_at() == T0


def test_spend_copy_is_incremental_paged_and_idempotent():
    litellm = FakeLiteLLM([litellm_row(n, T0 + timedelta(seconds=n)) for n in range(5)])
    store = FakeStore()
    copier = SpendCopier(litellm, store, batch=2)

    assert copier.run_once() == 5  # pages of 2, 2, 1
    assert [q[0] for q in litellm.queries] == [EPOCH] * 3

    litellm.rows.append(litellm_row(5, T0 + timedelta(seconds=5)))
    litellm.queries.clear()
    copier.run_once()

    newest_before = T0 + timedelta(seconds=4)
    assert litellm.queries[0][0] == newest_before - OVERLAP  # overlap re-read
    assert sorted(store.spend) == [f"req-{n}" for n in range(6)]  # no duplicates


def test_a_late_row_inside_the_overlap_is_not_missed():
    store = FakeStore()
    litellm = FakeLiteLLM([litellm_row(1, T0 + timedelta(minutes=10))])
    SpendCopier(litellm, store).run_once()

    # LiteLLM commits a batch whose created_at is 2 minutes older than the newest row
    litellm.rows.insert(0, litellm_row(2, T0 + timedelta(minutes=8)))
    SpendCopier(litellm, store).run_once()

    assert set(store.spend) == {"req-1", "req-2"}


def test_failed_spend_runs_are_counted_and_retried_next_interval():
    stop = threading.Event()
    calls = []

    class Copier:
        def run_once(self):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("litellm database down")
            stop.set()
            return 3

    errors = sample("o2c_llm_spend_copy_runs_total", result="error")
    oks = sample("o2c_llm_spend_copy_runs_total", result="ok")

    run_periodically(Copier(), 0, stop)

    assert len(calls) == 2
    assert sample("o2c_llm_spend_copy_runs_total", result="error") - errors == 1
    assert sample("o2c_llm_spend_copy_runs_total", result="ok") - oks == 1


# --- schemas and the read-only role ---


def test_setup_grants_select_on_analytics_only():
    statements = setup_statements('o2c"db')
    joined = "\n".join(statements)

    assert "CREATE SCHEMA IF NOT EXISTS raw" in joined
    assert "CREATE SCHEMA IF NOT EXISTS analytics" in joined
    assert "GRANT USAGE ON SCHEMA analytics TO grafana_ro" in joined
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO grafana_ro" in joined
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES "
        "TO grafana_ro" in joined
    )
    assert "default_transaction_read_only = on" in joined
    assert 'GRANT CONNECT ON DATABASE "o2c""db" TO grafana_ro' in joined
    grants = [s for s in statements if s.startswith("GRANT")]
    assert not any(" raw " in s or "public" in s for s in grants)


@pytest.mark.parametrize(
    ("password", "usable"),
    [("s3cret-Value", True), ("replace-me", False), ("", False), (None, False)],
)
def test_placeholder_passwords_are_not_used(password, usable):
    assert usable_password(password) is usable


class FakeConnection:
    def __init__(self):
        self.sql: list[str] = []
        self.connection = type("C", (), {"driver_connection": None})()

    def execute(self, statement, *args):
        self.sql.append(str(statement))
        return type("R", (), {"scalar": lambda self: "o2c"})()

    def exec_driver_sql(self, sql):
        self.sql.append(sql)


class FakeEngine:
    def __init__(self):
        self.conn = FakeConnection()

    @contextmanager
    def begin(self):
        yield self.conn


def test_setup_without_a_password_creates_schemas_and_tables_but_no_role():
    engine, created = FakeEngine(), []

    setup(engine, "replace-me", create_tables=created.append)

    assert created == [engine.conn]
    assert not any("grafana_ro" in s for s in engine.conn.sql)
    assert "CREATE SCHEMA IF NOT EXISTS analytics" in engine.conn.sql
    # columns added after the first version of raw.llm_spend
    assert (
        "ALTER TABLE raw.llm_spend ADD COLUMN IF NOT EXISTS session_id text"
        in engine.conn.sql
    )
    assert "ALTER TABLE raw.llm_spend ADD COLUMN IF NOT EXISTS service text" in (
        engine.conn.sql
    )


def test_setup_with_a_password_creates_the_role(monkeypatch):
    engine = FakeEngine()

    class Composed:
        def __init__(self, *parts):
            self.parts = parts

        def format(self, *args):
            return Composed(*args)

        def as_string(self, conn):
            return "ALTER ROLE grafana_ro WITH LOGIN PASSWORD <literal>"

    import psycopg

    monkeypatch.setattr(psycopg.sql, "SQL", Composed)

    setup(engine, "s3cret", create_tables=lambda conn: None)

    executed = "\n".join(engine.conn.sql)
    assert "CREATE ROLE grafana_ro LOGIN" in executed
    assert executed.endswith("ALTER ROLE grafana_ro WITH LOGIN PASSWORD <literal>")
    assert "s3cret" not in executed  # only through psycopg's quoting


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("EVENTS_DATABASE_URL", "postgresql+psycopg://u:p@h/o2c")
    monkeypatch.delenv("LITELLM_DATABASE_URL", raising=False)
    s = app.Settings(_env_file=None)

    assert s.health_port == 8310
    assert s.spend_interval_seconds == 60.0
    assert s.litellm_database_url == ""


def test_raw_tables_are_in_the_raw_schema():
    assert {t.schema for t in (events, dlq, llm_spend)} == {"raw"}
    assert db.BI_ROLE == "grafana_ro"


def test_dlq_row_stores_the_logical_dlq_topic_in_every_environment(monkeypatch):
    monkeypatch.setenv("O2C_ENV", "dev")
    row = dlq_row(FakeMessage(b"{}", topic="dev.orders.dlq"))
    assert row["topic"] == "orders.dlq"  # dbt and Grafana see one name (ADR-017)
