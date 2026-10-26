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

from o2c_common import metrics
from o2c_common.events import Envelope
from o2c_common.kafka_config import client_config
from o2c_common.topics import environment, logical, physical

log = logging.getLogger(__name__)
DLQ_TOPIC = "orders.dlq"


class PermanentError(Exception):
    """Retrying will not help (e.g. invalid data). Goes straight to the DLQ."""


class EventProducer:
    """Sends to logical topic names; with an environment (`env`, else $O2C_ENV) the
    Kafka topic is prefixed: send("docs.received") goes to dev.docs.received."""

    def __init__(self, bootstrap: str, client_id: str, env: str | None = None):
        self.env = environment(env)
        self._producer = Producer(
            client_config(
                bootstrap,
                {
                    "client.id": client_id,
                    "enable.idempotence": True,  # no duplicates from producer retries
                    "acks": "all",  # wait until Kafka has stored the message safely
                },
            )
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
            physical(topic, self.env),
            key=key,
            value=value,
            headers=headers,
            on_delivery=self._on_delivery,
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
    """Consumes logical topics as a logical group. With an environment (`env`, else
    $O2C_ENV) Kafka sees prefixed names (dev.docs.received, group dev.classifier), and
    so does the processed-events store, which environments may share. Metrics labels
    and DLQ headers keep the logical names."""

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
        env: str | None = None,
    ):
        self.bootstrap, self.group_id, self.topics = bootstrap, group_id, topics
        self.env = environment(env)
        self.kafka_group = physical(group_id, self.env)
        self.handler, self.producer, self.store = handler, producer, store
        self.max_attempts, self.base_delay = max_attempts, base_delay
        metrics.init_consumer_metrics(group_id)

    def run(self, max_messages: int | None = None) -> None:
        consumer = Consumer(
            client_config(
                self.bootstrap,
                {
                    "group.id": self.kafka_group,
                    "enable.auto.commit": False,  # commit only after successful processing
                    "auto.offset.reset": "earliest",
                },
            )
        )
        consumer.subscribe([physical(topic, self.env) for topic in self.topics])
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
        group = (
            self.kafka_group
        )  # processed-events store: one key space per environment
        metrics.EVENTS_CONSUMED.labels(self.group_id).inc()
        try:
            event = Envelope.from_bytes(msg.value())
        except ValidationError as exc:
            self._to_dlq(
                msg,
                f"invalid envelope: {exc.errors()[0]['msg']}",
                "invalid_envelope",
            )
            return

        if self.store.seen(group, event.event_id):
            log.info("duplicate %s skipped", event.event_id)
            metrics.EVENTS_DUPLICATE.labels(self.group_id).inc()
            return

        error: Exception | None = None
        reason = "retries_exhausted"
        for attempt in range(1, self.max_attempts + 1):
            start = time.perf_counter()
            try:
                self.handler(event)
                self.producer.flush()  # outgoing events are stored before we mark and commit
                self.store.mark(group, event.event_id)
                metrics.HANDLER_SECONDS.labels(self.group_id).observe(
                    time.perf_counter() - start
                )
                return
            except PermanentError as exc:
                self._attempt_failed(start)
                error, reason = exc, "permanent_error"
                break
            except Exception as exc:  # noqa: BLE001 - any other error is retried
                self._attempt_failed(start)
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
        self._to_dlq(msg, repr(error), reason)

    def _attempt_failed(self, start: float) -> None:
        metrics.EVENTS_FAILED.labels(self.group_id).inc()
        metrics.HANDLER_SECONDS.labels(self.group_id).observe(
            time.perf_counter() - start
        )

    def _to_dlq(self, msg: Message, error: str, reason: str) -> None:
        headers = [
            ("original_topic", logical(msg.topic(), self.env).encode()),
            ("original_partition", str(msg.partition()).encode()),
            ("original_offset", str(msg.offset()).encode()),
            ("consumer_group", self.group_id.encode()),
            ("error", error[:1000].encode()),
            ("failed_at", datetime.now(UTC).isoformat().encode()),
        ]
        self.producer.send_raw(DLQ_TOPIC, msg.key(), msg.value(), headers)
        self.producer.flush()
        metrics.EVENTS_DLQ.labels(self.group_id, reason).inc()


class RawMessageConsumer(EventConsumer):
    """An EventConsumer for topics whose messages need not be Envelopes, i.e. the DLQ
    (invalid envelopes end up there). The handler gets the Kafka Message and must be
    idempotent by itself (key: topic, partition, offset); `store` is not used.

    A failing message is never sent to the DLQ (copying the DLQ into the DLQ would
    loop): it is retried with backoff (at most max_delay seconds apart) until it
    succeeds, so nothing is skipped while e.g. the database is down."""

    def __init__(
        self,
        *,
        bootstrap: str,
        group_id: str,
        topics: list[str],
        handler: Callable[[Message], None],
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        env: str | None = None,
    ):
        super().__init__(
            bootstrap=bootstrap,
            group_id=group_id,
            topics=topics,
            handler=handler,
            producer=None,
            store=InMemoryStore(),
            base_delay=base_delay,
            env=env,
        )
        self.max_delay = max_delay

    def process(self, msg: Message) -> None:
        metrics.EVENTS_CONSUMED.labels(self.group_id).inc()
        attempt = 0
        while True:
            start = time.perf_counter()
            try:
                self.handler(msg)
                metrics.HANDLER_SECONDS.labels(self.group_id).observe(
                    time.perf_counter() - start
                )
                return
            except Exception as exc:  # noqa: BLE001 - retried until it succeeds
                self._attempt_failed(start)
                attempt += 1
                delay = min(self.base_delay * 2 ** (attempt - 1), self.max_delay)
                log.warning(
                    "attempt %s failed for %s[%s]@%s: %s (retry in %.0fs)",
                    attempt,
                    msg.topic(),
                    msg.partition(),
                    msg.offset(),
                    exc,
                    delay,
                )
                time.sleep(delay)
