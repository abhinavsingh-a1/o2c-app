import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from confluent_kafka import Consumer, Message, Producer
from pydantic import ValidationError
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, select
from sqlalchemy.dialects.postgresql import insert

from o2c_common.events import Envelope

log = logging.getLogger(__name__)
DLQ_TOPIC = "orders.dlq"


class PermanentError(Exception):
    """Retrying will not help (e.g. invalid data). Goes straight to the DLQ."""


class EventProducer:
    def __init__(self, bootstrap: str, client_id: str):
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "client.id": client_id,
                "enable.idempotence": True,  # no duplicates from producer retries
                "acks": "all",  # wait until Kafka has stored the message safely
            }
        )
        self._errors: list[str] = []

    def _on_delivery(self, err, msg) -> None:
        if err is not None:
            self._errors.append(f"{msg.topic()}: {err}")

    def send(self, topic: str, event: Envelope) -> None:
        self.send_raw(topic, event.key, event.to_bytes())

    def send_raw(
        self,
        topic: str,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self._producer.produce(
            topic, key=key, value=value, headers=headers, on_delivery=self._on_delivery
        )
        self._producer.poll(0)

    def flush(self, timeout: float = 10.0) -> None:
        if self._producer.flush(timeout) > 0:
            raise RuntimeError("messages not delivered before timeout")
        if self._errors:
            errors, self._errors = self._errors, []
            raise RuntimeError(f"delivery failed: {errors}")


class ProcessedStore(Protocol):
    def seen(self, group: str, event_id: UUID) -> bool: ...
    def mark(self, group: str, event_id: UUID) -> None: ...


class InMemoryStore:
    """For tests and smoke runs only."""

    def __init__(self) -> None:
        self._done: set[tuple[str, UUID]] = set()

    def seen(self, group: str, event_id: UUID) -> bool:
        return (group, event_id) in self._done

    def mark(self, group: str, event_id: UUID) -> None:
        self._done.add((group, event_id))


_metadata = MetaData()
processed_events = Table(
    "processed_events",
    _metadata,
    Column("consumer_group", String(100), primary_key=True),
    Column("event_id", String(36), primary_key=True),
    Column("processed_at", DateTime(timezone=True), nullable=False),
)


class PostgresStore:
    def __init__(self, database_url: str):
        self._engine = create_engine(database_url)
        _metadata.create_all(self._engine)

    def seen(self, group: str, event_id: UUID) -> bool:
        stmt = select(processed_events.c.event_id).where(
            processed_events.c.consumer_group == group,
            processed_events.c.event_id == str(event_id),
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def mark(self, group: str, event_id: UUID) -> None:
        stmt = (
            insert(processed_events)
            .values(
                consumer_group=group,
                event_id=str(event_id),
                processed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)


class EventConsumer:
    def __init__(
        self,
        *,
        bootstrap: str,
        group_id: str,
        topics: list[str],
        handler: Callable[[Envelope], None],
        producer: EventProducer,
        store: ProcessedStore,
        max_attempts: int = 3,
        base_delay: float = 1.0,
    ):
        self.bootstrap, self.group_id, self.topics = bootstrap, group_id, topics
        self.handler, self.producer, self.store = handler, producer, store
        self.max_attempts, self.base_delay = max_attempts, base_delay

    def run(self, max_messages: int | None = None) -> None:
        consumer = Consumer(
            {
                "bootstrap.servers": self.bootstrap,
                "group.id": self.group_id,
                "enable.auto.commit": False,  # we commit only after successful processing
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe(self.topics)
        handled = 0
        try:
            while max_messages is None or handled < max_messages:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.error("kafka error: %s", msg.error())
                    continue
                self.process(msg)
                consumer.commit(message=msg, asynchronous=False)
                handled += 1
        finally:
            consumer.close()

    def process(self, msg: Message) -> None:
        try:
            event = Envelope.from_bytes(msg.value())
        except ValidationError as exc:
            self._to_dlq(msg, f"invalid envelope: {exc.errors()[0]['msg']}")
            return

        if self.store.seen(self.group_id, event.event_id):
            log.info("duplicate %s skipped", event.event_id)
            return

        error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.handler(event)
                self.producer.flush()  # outgoing events are stored before we mark and commit
                self.store.mark(self.group_id, event.event_id)
                return
            except PermanentError as exc:
                error = exc
                break
            except Exception as exc:  # noqa: BLE001 - any other error is retried
                error = exc
                log.warning(
                    "attempt %s/%s failed for %s: %s",
                    attempt,
                    self.max_attempts,
                    event.event_id,
                    exc,
                )
                if attempt < self.max_attempts:
                    time.sleep(self.base_delay * 2 ** (attempt - 1))
        self._to_dlq(msg, repr(error))

    def _to_dlq(self, msg: Message, error: str) -> None:
        headers = [
            ("original_topic", msg.topic().encode()),
            ("original_partition", str(msg.partition()).encode()),
            ("original_offset", str(msg.offset()).encode()),
            ("consumer_group", self.group_id.encode()),
            ("error", error[:1000].encode()),
            ("failed_at", datetime.now(UTC).isoformat().encode()),
        ]
        self.producer.send_raw(DLQ_TOPIC, msg.key(), msg.value(), headers)
        self.producer.flush()
