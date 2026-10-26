"""The raw schema, the read-only BI role and the writes of the analytics sink (ADR-013).

raw.* holds the data as it arrived (one row per event, DLQ message or LiteLLM spend log
row); dbt (analytics/dbt) builds the analytics.* marts from it. Every write is an
INSERT ... ON CONFLICT DO NOTHING on the natural key, so a redelivered message or a
re-copied spend row never creates a second row.
"""

import logging
from datetime import UTC, datetime

from confluent_kafka import Message
from o2c_common.events import Envelope
from o2c_common.topics import environment, logical
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

log = logging.getLogger(__name__)

RAW, ANALYTICS = "raw", "analytics"
BI_ROLE = "grafana_ro"
PLACEHOLDER = "replace-me"

metadata = MetaData(schema=RAW)
events = Table(
    "events",
    metadata,
    Column("event_id", UUID(as_uuid=False), primary_key=True),
    # The Kafka topic. Every O2C event is published to the topic named like its
    # event_type (docs.received, orders.posted, ...).
    Column("topic", String(100), nullable=False),
    Column("event_type", String(100), nullable=False, index=True),
    Column("schema_version", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(64), nullable=False, index=True),
    Column("correlation_id", String(64), nullable=False),
    Column("causation_id", UUID(as_uuid=False)),
    Column("producer", String(50), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)
dlq = Table(
    "dlq",
    metadata,
    Column("topic", String(100), primary_key=True),
    Column("kafka_partition", Integer, primary_key=True),
    Column("kafka_offset", BigInteger, primary_key=True),
    Column("kafka_key", Text),
    Column("value", LargeBinary),  # as received: may not even be valid JSON
    Column("headers", JSONB, nullable=False),  # original_topic, error, failed_at, ...
    Column("kafka_timestamp", DateTime(timezone=True)),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)
llm_spend = Table(
    "llm_spend",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("start_time", DateTime(timezone=True), nullable=False),
    Column("spend_usd", Float, nullable=False),
    Column("model", Text, nullable=False),
    Column("model_group", Text),  # the LiteLLM alias, e.g. o2c-extract
    Column("key_alias", Text),  # the virtual key (ADR-011); NULL for the master key
    # Langfuse session id: the order's idempotency key for the calls a service makes for
    # one document (classify, extract, explain), so cost can be attributed per order.
    Column("session_id", Text, index=True),
    # The calling service from the request tags (extractor, classifier, agent, eval,
    # untagged). NULL only in rows copied before this column existed.
    Column("service", Text),
    # LiteLLM's insert time (UTC, without time zone as in LiteLLM's table): the
    # watermark of the incremental copy.
    Column("source_created_at", DateTime(timezone=False), nullable=False, index=True),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)


def event_row(event: Envelope) -> dict:
    return {
        "event_id": str(event.event_id),
        "topic": event.event_type,
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "occurred_at": event.occurred_at,
        "idempotency_key": event.idempotency_key,
        "correlation_id": event.correlation_id,
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "producer": event.producer,
        "payload": event.payload,
        "ingested_at": datetime.now(UTC),
    }


def _decode(value: bytes | None) -> str | None:
    return None if value is None else value.decode("utf-8", errors="replace")


def dlq_row(msg: Message) -> dict:
    kind, millis = msg.timestamp() if hasattr(msg, "timestamp") else (0, None)
    return {
        "topic": logical(msg.topic(), environment()),  # orders.dlq in every environment
        "kafka_partition": msg.partition(),
        "kafka_offset": msg.offset(),
        "kafka_key": _decode(msg.key()),
        "value": msg.value(),
        "headers": {name: _decode(value) for name, value in (msg.headers() or [])},
        "kafka_timestamp": (
            datetime.fromtimestamp(millis / 1000, UTC) if kind and millis else None
        ),
        "ingested_at": datetime.now(UTC),
    }


class Store:
    """Writes into raw.* of the o2c database."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> "Store":
        return cls(create_engine(database_url, pool_pre_ping=True))

    def insert_event(self, row: dict) -> None:
        self._insert(events, [row])

    def insert_dlq(self, row: dict) -> None:
        self._insert(dlq, [row])

    def insert_spend(self, rows: list[dict]) -> None:
        if rows:
            with self.engine.begin() as conn:
                conn.execute(upsert_spend(), rows)

    def max_spend_created_at(self) -> datetime | None:
        """The watermark: the newest copied row that has all columns. Rows copied
        before session_id/service existed do not count, so they are read again once
        and completed by upsert_spend."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.max(llm_spend.c.source_created_at)).where(
                    llm_spend.c.service.is_not(None)
                )
            ).scalar()

    def _insert(self, table: Table, rows: list[dict]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert_ignoring_duplicates(table), rows)


def upsert_spend():
    """Insert spend rows; a row already copied is only completed (session_id, service)
    if it predates those columns, otherwise left as it is. Idempotent."""
    statement = insert(llm_spend)
    return statement.on_conflict_do_update(
        index_elements=["request_id"],
        set_={
            "session_id": statement.excluded.session_id,
            "service": statement.excluded.service,
        },
        where=llm_spend.c.service.is_(None),
    )


def insert_ignoring_duplicates(table: Table):
    return insert(table).on_conflict_do_nothing(
        index_elements=[c.name for c in table.primary_key.columns]
    )


# --- schemas and the read-only role ---


# Columns added after raw.llm_spend was first created (create_all does not alter tables)
MIGRATIONS = [
    f"ALTER TABLE {RAW}.llm_spend ADD COLUMN IF NOT EXISTS session_id text",
    f"ALTER TABLE {RAW}.llm_spend ADD COLUMN IF NOT EXISTS service text",
    f"CREATE INDEX IF NOT EXISTS ix_raw_llm_spend_session_id ON {RAW}.llm_spend (session_id)",
]


def setup_statements(database: str) -> list[str]:
    """DDL and grants without the password; idempotent (run at every start)."""
    db = '"' + database.replace('"', '""') + '"'
    return [
        f"CREATE SCHEMA IF NOT EXISTS {RAW}",
        f"CREATE SCHEMA IF NOT EXISTS {ANALYTICS}",
        (
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{BI_ROLE}') THEN "
            f"CREATE ROLE {BI_ROLE} LOGIN; END IF; END $$"
        ),
        # Read-only even if someone grants it more later
        f"ALTER ROLE {BI_ROLE} SET default_transaction_read_only = on",
        f"GRANT CONNECT ON DATABASE {db} TO {BI_ROLE}",
        # analytics only: not raw (payloads with customer data), not public (reviews,
        # checkpoints, audit trail)
        f"REVOKE ALL ON SCHEMA {RAW} FROM {BI_ROLE}",
        f"GRANT USAGE ON SCHEMA {ANALYTICS} TO {BI_ROLE}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {ANALYTICS} TO {BI_ROLE}",
        # Tables and views dbt creates later (as this same database user)
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {ANALYTICS} GRANT SELECT ON TABLES TO {BI_ROLE}",
    ]


def usable_password(password: str | None) -> bool:
    return bool(password) and PLACEHOLDER not in password


def setup(engine: Engine, bi_password: str | None, create_tables=None) -> None:
    """Create raw/analytics, the raw tables and grafana_ro (with SELECT on analytics
    only). Without a real GRAFANA_DB_PASSWORD the role is not created or changed."""
    from psycopg import sql

    with engine.begin() as conn:
        conn.execute(CreateSchema(RAW, if_not_exists=True))
        (create_tables or metadata.create_all)(conn)
        for statement in MIGRATIONS:
            conn.execute(text(statement))
        if not usable_password(bi_password):
            log.warning(
                "GRAFANA_DB_PASSWORD not set: role %s not created/updated", BI_ROLE
            )
            conn.execute(CreateSchema(ANALYTICS, if_not_exists=True))
            return
        database = conn.execute(text("SELECT current_database()")).scalar()
        for statement in setup_statements(database):
            conn.execute(text(statement))
        # The password as a quoted literal (ALTER ROLE takes no bind parameters)
        raw = conn.connection.driver_connection
        conn.exec_driver_sql(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
            .format(sql.Identifier(BI_ROLE), sql.Literal(bi_password))
            .as_string(raw)
        )
