# E6 Execution Guide — Event Backbone (Kafka)

How the Kafka event backbone of the Order-to-Cash Agent was built, in the order it was done, with the **final working version** of every file. Problems and fixes are in **Troubleshooting** at the end. Everything runs in the `o2c-app` repo on the local stack.

**Result of E6:** 7 topics defined in code; a standard event envelope with a causation chain; one shared producer/consumer library with retries, dead-letter queue (DLQ) and idempotent processing; a DLQ replay tool; lag check and local topic reset.

**Flow:** intake → `docs.received` → classification → `docs.classified` → extraction → `orders.extracted` → agent → `orders.posted` / `orders.exceptions`. Failures from any consumer → `orders.dlq`.

## Step 1 — Topic design (ADR-003)

```bash
git checkout -b feat/event-backbone
uv add --package o2c-common confluent-kafka
```

`docs/adr/0003-events-and-topics.md` records the decisions:

| Topic | Producer | Consumer | Retention |
| --- | --- | --- | --- |
| docs.received | intake | classification | 7 days |
| docs.classified | classification | extraction | 7 days |
| orders.extracted | extraction | agent | 7 days |
| orders.validated | agent | (audit) | 7 days |
| orders.exceptions | agent | review UI, ServiceNow | 30 days |
| orders.posted | agent | CRM (Zapier) | 30 days |
| orders.dlq | any consumer | DLQ replay tool | 30 days |

- Message key = document idempotency key → all events of one PO stay in one partition, in order.
- 3 partitions; replication 1 locally, 3 in AWS.
- **At-least-once delivery:** a message can arrive twice, so every consumer must be idempotent.
- After N failed attempts a message goes to `orders.dlq` with error headers.

**`libs/o2c-common/src/o2c_common/topics.py`** (final, including reset with a localhost guard):

```python
import os
import sys
import time

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

DAY_MS = 24 * 60 * 60 * 1000
PARTITIONS = 3
TOPICS = {
    "docs.received": 7,
    "docs.classified": 7,
    "orders.extracted": 7,
    "orders.validated": 7,
    "orders.exceptions": 30,
    "orders.posted": 30,
    "orders.dlq": 30,
}


def create_topics(bootstrap: str, replication: int = 1) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    new_topics = [
        NewTopic(
            name,
            num_partitions=PARTITIONS,
            replication_factor=replication,
            config={"retention.ms": str(days * DAY_MS)},
        )
        for name, days in TOPICS.items()
    ]
    for name, future in admin.create_topics(new_topics).items():
        try:
            future.result()
            print(f"created {name}")
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.TOPIC_ALREADY_EXISTS:
                print(f"exists  {name}")
            else:
                raise


def delete_topics(bootstrap: str) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    for name, future in admin.delete_topics(list(TOPICS)).items():
        try:
            future.result()
            print(f"deleted {name}")
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                print(f"missing {name}")
            else:
                raise


if __name__ == "__main__":
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
    if "--reset" in sys.argv:
        if "localhost" not in bootstrap:
            sys.exit("refusing to reset topics outside localhost")
        delete_topics(bootstrap)
        time.sleep(5)
        create_topics(bootstrap)
    else:
        create_topics(bootstrap)
```

Verify: `docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic orders.dlq` → `PartitionCount: 3`, `retention.ms=2592000000`.

## Step 2 — Event envelope

| Field | Purpose |
| --- | --- |
| `event_id` | Unique message ID (deterministic for child events) |
| `event_type` | What happened |
| `schema_version` | Format version |
| `occurred_at` | UTC timestamp |
| `producer` | Sending service |
| `idempotency_key` | Document identity; also the Kafka key |
| `correlation_id` | Ties all logs/traces of one PO together |
| `causation_id` | Parent event → audit chain |
| `payload` | Content |

**`libs/o2c-common/src/o2c_common/events.py`** (final):

```python
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field


class Envelope(BaseModel):
    """Standard wrapper for every Kafka message."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(pattern=r"^[a-z]+\.[a-z_.]+$")
    schema_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    producer: str
    idempotency_key: str = Field(min_length=16, max_length=64)
    correlation_id: str
    causation_id: UUID | None = None
    payload: dict[str, Any]

    @classmethod
    def new(
        cls,
        event_type: str,
        producer: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> "Envelope":
        return cls(
            event_type=event_type,
            producer=producer,
            idempotency_key=idempotency_key,
            correlation_id=idempotency_key,
            payload=payload,
        )

    def child(
        self, event_type: str, producer: str, payload: dict[str, Any]
    ) -> "Envelope":
        return Envelope(
            event_id=uuid5(NAMESPACE_URL, f"{self.event_id}/{event_type}"),
            event_type=event_type,
            producer=producer,
            idempotency_key=self.idempotency_key,
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            payload=payload,
        )

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Envelope":
        return cls.model_validate_json(data)

    @property
    def key(self) -> bytes:
        return self.idempotency_key.encode()


if __name__ == "__main__":
    out = Path(__file__).parents[2] / "schemas" / "envelope.schema.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(Envelope.model_json_schema(), indent=2) + "\n")
    print(f"wrote {out}")
```

Design choices: `frozen=True` (events are facts); `extra="ignore"` (tolerant reader: older consumers survive new fields); `child()` keeps the keys and derives the new ID from the parent, so re-processing a message produces the **same** child event and downstream consumers recognise the duplicate.

Tests in `libs/o2c-common/tests/test_events.py`: byte round-trip, child keys and causation, deterministic child ID, unknown fields ignored, missing payload rejected, bad event type rejected, immutability, UTC timestamp. `uv run python -m o2c_common.events` writes `libs/o2c-common/schemas/envelope.schema.json`.

## Step 3 — Shared producer and consumer

```bash
uv add --package o2c-common sqlalchemy "psycopg[binary]"
```

Processing order per message: validate envelope (invalid → DLQ) → skip if already processed → handler with retries (1 s, 2 s, 4 s) → permanent error or retries exhausted → DLQ with headers → on success: flush outgoing events, mark processed, **then** commit.

**`libs/o2c-common/src/o2c_common/kafka.py`**:

```python
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
                "enable.idempotence": True,
                "acks": "all",
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
                "enable.auto.commit": False,
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
                self.producer.flush()
                self.store.mark(self.group_id, event.event_id)
                return
            except PermanentError as exc:
                error = exc
                break
            except Exception as exc:  # noqa: BLE001
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
```

Key settings: `enable.auto.commit: False` (commit only after success); `enable.idempotence` + `acks: all` (no lost or duplicated produces). DLQ messages keep the original bytes and are **not** marked processed, so a replay is processed normally. Unit tests (`tests/test_kafka.py`, written by Claude Code) use a fake message, fake producer, `InMemoryStore` and `base_delay=0`.

Smoke test `local/kafka_smoke.py` sends one event and consumes it with a handler that always fails; after 3 attempts the message appears in `orders.dlq` with headers:

```bash
uv run python local/kafka_smoke.py
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic orders.dlq --from-beginning --property print.headers=true --timeout-ms 5000
```

## Step 4 — DLQ replay tool

Kafka stores read **positions**, not per-message flags, so the tool replays everything it reads; idempotent consumers make that safe.

**`libs/o2c-common/src/o2c_common/dlq.py`**:

```python
import argparse
import os
import time
from datetime import UTC, datetime

from confluent_kafka import Consumer, Message

from o2c_common.kafka import DLQ_TOPIC, EventProducer


def read_headers(msg: Message) -> dict[str, str]:
    return {key: (value or b"").decode() for key, value in (msg.headers() or [])}


def replay(
    bootstrap: str, dry_run: bool, max_messages: int, idle_seconds: float = 5.0
) -> int:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "dlq-replay",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([DLQ_TOPIC])
    producer = EventProducer(bootstrap, "dlq-replay")
    count, last_message = 0, time.monotonic()
    try:
        while count < max_messages:
            msg = consumer.poll(1.0)
            if msg is None:
                if time.monotonic() - last_message > idle_seconds:
                    break
                continue
            last_message = time.monotonic()
            if msg.error():
                continue
            headers = read_headers(msg)
            target = headers.get("original_topic")
            print(
                f"{'WOULD REPLAY' if dry_run else 'REPLAY'} -> {target} | "
                f"failed_at={headers.get('failed_at')} | error={headers.get('error', '')[:80]}"
            )
            count += 1
            if dry_run or not target:
                continue
            producer.send_raw(
                target,
                msg.key(),
                msg.value(),
                [
                    ("replayed_from_dlq", b"true"),
                    ("replayed_at", datetime.now(UTC).isoformat().encode()),
                ],
            )
            producer.flush()
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay messages from the dead-letter queue."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only list, change nothing"
    )
    parser.add_argument(
        "--max", type=int, default=1000, help="maximum messages to handle"
    )
    args = parser.parse_args()
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
    count = replay(bootstrap, args.dry_run, args.max)
    print(f"{count} message(s) {'listed' if args.dry_run else 'replayed'}")


if __name__ == "__main__":
    main()
```

Verified: `make dlq-list` twice lists the same message; `make dlq-replay` sends it back; `docs.received` then contains the original and the replayed copy (header `replayed_from_dlq:true`) with the **same** `event_id`.

## Step 5 — Lag check, reset and wrap-up

- `make lag`: `kafka-consumer-groups.sh --describe --all-groups`; the **LAG** column shows unprocessed messages per group.
- `make topics-reset`: deletes and recreates the 7 topics (refuses anything except localhost); the E2 `smoke-test` topic was deleted manually.
- CLAUDE.md rules: every consumer uses `EventConsumer`; every event is an `Envelope`; outgoing events use `envelope.child(...)`.

**Makefile targets added in E6:** `topics`, `topics-reset`, `dlq-list`, `dlq-replay`, `lag`.

## Troubleshooting — problems hit during E6 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `warning: Failed to hardlink files` again after `make sync` | Git Bash does not read the `UV_LINK_MODE` variable set in PowerShell | `echo 'export UV_LINK_MODE=copy' >> ~/.bashrc` and `source ~/.bashrc` |
| 2 | Console consumer ends with `ERROR … TimeoutException` | Normal stop when `--timeout-ms` runs out | Harmless; check `Processed a total of N messages` |
| 3 | Unclear which repo E6 runs in | Three repos in the workspace | Everything in E6 runs in `o2c-app` |
| 4 | A re-processed message would create a second child event with a new ID | `child()` used random IDs | Child ID derived with `uuid5` from the parent ID and event type |
| 5 | Test messages would reach the real services in E7 | Smoke tests wrote to the project topics | `make topics-reset` before starting E7 |

**Lessons:**

- Commit offsets only after the work and its outgoing events are safe.
- At-least-once delivery requires idempotent consumers and deterministic child IDs.
- A DLQ keeps one bad message from blocking everything; a replay tool makes it recoverable.
- Consumer lag is the key Kafka health metric.
