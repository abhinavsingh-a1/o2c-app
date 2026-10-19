from datetime import datetime

import pytest
from o2c_common.events import Envelope
from o2c_common.kafka import DLQ_TOPIC, EventConsumer, InMemoryStore, PermanentError

GROUP = "test-group"
TOPIC = "docs.received"


class FakeMessage:
    def __init__(
        self,
        value: bytes,
        key: bytes | None = b"key",
        topic: str = TOPIC,
        partition: int = 2,
        offset: int = 42,
    ):
        self._value, self._key = value, key
        self._topic, self._partition, self._offset = topic, partition, offset

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes | None:
        return self._key

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes | None, bytes | None, list | None]] = []
        self.flushes = 0

    def send_raw(self, topic, key, value, headers=None) -> None:
        self.sent.append((topic, key, value, headers))

    def flush(self, timeout: float = 10.0) -> None:
        self.flushes += 1


class Handler:
    """Records every call; raises the given errors in order, then succeeds."""

    def __init__(self, *errors: Exception):
        self.errors = list(errors)
        self.calls: list[Envelope] = []

    def __call__(self, event: Envelope) -> None:
        self.calls.append(event)
        if self.errors:
            raise self.errors.pop(0)


def make_event() -> Envelope:
    return Envelope.new(TOPIC, "intake", "k" * 64, {"s3_key": "po.pdf"})


@pytest.fixture
def producer():
    return FakeProducer()


@pytest.fixture
def store():
    return InMemoryStore()


def make_consumer(handler, producer, store, max_attempts: int = 3) -> EventConsumer:
    return EventConsumer(
        bootstrap="unused:9092",
        group_id=GROUP,
        topics=[TOPIC],
        handler=handler,
        producer=producer,
        store=store,
        max_attempts=max_attempts,
        base_delay=0,
    )


def dlq_messages(producer: FakeProducer) -> list[tuple]:
    return [sent for sent in producer.sent if sent[0] == DLQ_TOPIC]


def test_success_calls_handler_once_and_marks_event(producer, store):
    event, handler = make_event(), Handler()
    make_consumer(handler, producer, store).process(FakeMessage(event.to_bytes()))
    assert handler.calls == [event]
    assert store.seen(GROUP, event.event_id)
    assert dlq_messages(producer) == []


def test_duplicate_is_skipped(producer, store):
    event, handler = make_event(), Handler()
    consumer = make_consumer(handler, producer, store)
    consumer.process(FakeMessage(event.to_bytes(), offset=1))
    consumer.process(FakeMessage(event.to_bytes(), offset=2))  # redelivered
    assert len(handler.calls) == 1
    assert dlq_messages(producer) == []


def test_transient_error_succeeds_on_second_attempt(producer, store):
    event, handler = make_event(), Handler(ConnectionError("ERP timeout"))
    make_consumer(handler, producer, store).process(FakeMessage(event.to_bytes()))
    assert len(handler.calls) == 2
    assert store.seen(GROUP, event.event_id)
    assert dlq_messages(producer) == []


def test_permanent_error_goes_to_dlq_without_retries(producer, store):
    event, handler = make_event(), Handler(PermanentError("unknown customer"))
    make_consumer(handler, producer, store).process(FakeMessage(event.to_bytes()))
    assert len(handler.calls) == 1
    [(_, _, _, headers)] = dlq_messages(producer)
    assert "unknown customer" in dict(headers)["error"].decode()


def test_exhausted_retries_go_to_dlq_with_all_headers(producer, store):
    event = make_event()
    handler = Handler(*(RuntimeError(f"boom {i}") for i in range(3)))
    msg = FakeMessage(event.to_bytes(), key=b"po-key", partition=2, offset=42)
    make_consumer(handler, producer, store, max_attempts=3).process(msg)

    assert len(handler.calls) == 3
    [(topic, key, value, headers)] = dlq_messages(producer)
    assert (topic, key, value) == (DLQ_TOPIC, b"po-key", event.to_bytes())
    headers = dict(headers)
    assert list(headers) == [
        "original_topic",
        "original_partition",
        "original_offset",
        "consumer_group",
        "error",
        "failed_at",
    ]
    assert headers["original_topic"] == TOPIC.encode()
    assert headers["original_partition"] == b"2"
    assert headers["original_offset"] == b"42"
    assert headers["consumer_group"] == GROUP.encode()
    assert headers["error"] == repr(RuntimeError("boom 2")).encode()  # the last error
    assert (
        datetime.fromisoformat(headers["failed_at"].decode())
        .utcoffset()
        .total_seconds()
        == 0
    )


def test_invalid_envelope_goes_to_dlq(producer, store):
    handler = Handler()
    make_consumer(handler, producer, store).process(
        FakeMessage(b'{"not": "an envelope"}')
    )
    assert handler.calls == []
    [(_, _, value, headers)] = dlq_messages(producer)
    assert value == b'{"not": "an envelope"}'
    assert dict(headers)["error"].startswith(b"invalid envelope: ")


def test_dlq_event_is_not_marked_as_processed(producer, store):
    event, handler = make_event(), Handler(PermanentError("bad data"))
    make_consumer(handler, producer, store).process(FakeMessage(event.to_bytes()))
    assert len(dlq_messages(producer)) == 1
    assert not store.seen(GROUP, event.event_id)
