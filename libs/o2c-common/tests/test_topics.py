"""The environment prefix (ADR-017) in topics, EventProducer, EventConsumer and the DLQ tool."""

from typing import ClassVar

import pytest
from o2c_common import dlq, kafka, topics
from o2c_common.events import Envelope
from o2c_common.kafka import DLQ_TOPIC, EventConsumer, EventProducer, InMemoryStore
from o2c_common.metrics import sample
from o2c_common.topics import ENV_VAR, environment, logical, physical


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


# --- names --------------------------------------------------------------------


def test_without_o2c_env_names_are_unchanged():
    assert environment() is None
    assert physical("docs.received", None) == "docs.received"
    assert logical("docs.received", None) == "docs.received"


def test_environment_from_o2c_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "dev")
    assert environment() == "dev"


def test_explicit_env_wins_over_o2c_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "dev")
    assert environment("prod") == "prod"


def test_empty_o2c_env_means_no_prefix(monkeypatch):
    monkeypatch.setenv(ENV_VAR, " ")
    assert environment() is None


def test_unknown_environment_is_rejected(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "staging")
    with pytest.raises(ValueError, match="dev, test, prod"):
        environment()


def test_physical_and_logical_names():
    assert physical("docs.received", "dev") == "dev.docs.received"
    assert physical("classifier", "test") == "test.classifier"
    assert logical("dev.docs.received", "dev") == "docs.received"
    assert logical("docs.received", "dev") == "docs.received"  # unprefixed stays
    assert logical("test.docs.received", "dev") == "test.docs.received"


# --- topics script ------------------------------------------------------------


class FakeFuture:
    def result(self):
        return None


class FakeAdmin:
    created: ClassVar[list[str]] = []
    deleted: ClassVar[list[str]] = []

    def __init__(self, config):
        self.config = config

    def create_topics(self, new_topics):
        FakeAdmin.created = [t.topic for t in new_topics]
        return {name: FakeFuture() for name in FakeAdmin.created}

    def delete_topics(self, names):
        FakeAdmin.deleted = list(names)
        return {name: FakeFuture() for name in names}


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(topics, "AdminClient", FakeAdmin)
    monkeypatch.setattr(topics.time, "sleep", lambda s: None)
    FakeAdmin.created, FakeAdmin.deleted = [], []
    return FakeAdmin


def test_create_topics_with_env_prefix(admin):
    topics.create_topics("localhost:9092", env="dev")
    assert admin.created == [f"dev.{name}" for name in topics.TOPICS]


def test_topics_script_env_flag(admin):
    topics.main(["--env", "test"])
    assert admin.created[0] == "test.docs.received"


def test_topics_script_uses_o2c_env_by_default(admin, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "prod")
    topics.main([])
    assert "prod.orders.dlq" in admin.created


def test_topics_script_without_env_keeps_current_names(admin):
    topics.main([])
    assert admin.created == list(topics.TOPICS)


def test_reset_deletes_and_recreates_only_that_environment(admin):
    topics.main(["--env", "dev", "--reset"])
    assert admin.deleted == [f"dev.{name}" for name in topics.TOPICS]
    assert admin.created == admin.deleted


def test_reset_refuses_outside_localhost(admin, monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP", "kafka.prod:9092")
    with pytest.raises(SystemExit, match="refusing"):
        topics.main(["--reset"])
    assert admin.deleted == []


def test_topics_script_rejects_unknown_env(admin):
    with pytest.raises(SystemExit):
        topics.main(["--env", "staging"])


# --- producer and consumer ----------------------------------------------------


class FakeKafkaProducer:
    def __init__(self, config):
        self.produced = []

    def produce(self, topic, key, value, headers, on_delivery):
        self.produced.append(topic)

    def poll(self, timeout):
        return 0


def test_producer_prefixes_topics(monkeypatch):
    monkeypatch.setattr(kafka, "Producer", FakeKafkaProducer)
    monkeypatch.setenv(ENV_VAR, "dev")
    producer = EventProducer("unused:9092", "intake")

    producer.send_raw("docs.received", b"k", b"v")

    assert producer._producer.produced == ["dev.docs.received"]


def test_producer_without_env_keeps_the_topic(monkeypatch):
    monkeypatch.setattr(kafka, "Producer", FakeKafkaProducer)
    producer = EventProducer("unused:9092", "intake")

    producer.send_raw("docs.received", b"k", b"v")

    assert producer._producer.produced == ["docs.received"]


class FakeKafkaConsumer:
    instances: ClassVar[list["FakeKafkaConsumer"]] = []

    def __init__(self, config):
        self.config, self.subscribed = config, []
        FakeKafkaConsumer.instances.append(self)

    def subscribe(self, names):
        self.subscribed = names

    def poll(self, timeout):
        return None

    def close(self):
        pass


class Message:
    def __init__(self, value, topic):
        self._value, self._topic = value, topic

    def value(self):
        return self._value

    def key(self):
        return b"key"

    def topic(self):
        return self._topic

    def partition(self):
        return 0

    def offset(self):
        return 5


class RecordingProducer:
    def __init__(self):
        self.sent = []

    def send_raw(self, topic, key, value, headers=None):
        self.sent.append((topic, dict(headers or [])))

    def flush(self, timeout=10.0):
        pass


def make_consumer(handler, producer, store, env=None):
    return EventConsumer(
        bootstrap="unused:9092",
        group_id="classifier",
        topics=["docs.received"],
        handler=handler,
        producer=producer,
        store=store,
        max_attempts=1,
        base_delay=0,
        env=env,
    )


def test_consumer_subscribes_prefixed_topics_as_a_prefixed_group(monkeypatch):
    monkeypatch.setattr(kafka, "Consumer", FakeKafkaConsumer)
    monkeypatch.setenv(ENV_VAR, "dev")
    consumer = make_consumer(lambda e: None, RecordingProducer(), InMemoryStore())

    consumer.run(max_messages=0)

    kafka_consumer = FakeKafkaConsumer.instances[-1]
    assert kafka_consumer.config["group.id"] == "dev.classifier"
    assert kafka_consumer.subscribed == ["dev.docs.received"]


def test_processed_events_are_kept_per_environment():
    event = Envelope.new("docs.received", "intake", "k" * 64, {"s3_key": "po.pdf"})
    store = InMemoryStore()
    calls = []
    for env in ("dev", "test"):
        make_consumer(calls.append, RecordingProducer(), store, env=env).process(
            Message(event.to_bytes(), f"{env}.docs.received")
        )

    assert len(calls) == 2  # the same event in two environments is not a duplicate
    assert store.seen("dev.classifier", event.event_id)
    assert store.seen("test.classifier", event.event_id)


def test_metrics_and_dlq_headers_keep_logical_names():
    event = Envelope.new("docs.received", "intake", "k" * 64, {"s3_key": "po.pdf"})
    producer = RecordingProducer()
    before = sample(
        "o2c_events_dlq_total", group="classifier", reason="permanent_error"
    )

    def fail(event):
        raise kafka.PermanentError("bad")

    make_consumer(fail, producer, InMemoryStore(), env="prod").process(
        Message(event.to_bytes(), "prod.docs.received")
    )

    [(topic, headers)] = producer.sent
    assert topic == DLQ_TOPIC  # the producer adds the prefix
    assert headers["original_topic"] == b"docs.received"
    assert headers["consumer_group"] == b"classifier"
    after = sample("o2c_events_dlq_total", group="classifier", reason="permanent_error")
    assert after == before + 1


# --- DLQ tool -----------------------------------------------------------------


class DlqMessage(Message):
    def __init__(self, original_topic: bytes):
        super().__init__(b"value", "dev.orders.dlq")
        self._headers = [("original_topic", original_topic), ("error", b"boom")]

    def headers(self):
        return self._headers

    def error(self):
        return None


class DlqConsumer(FakeKafkaConsumer):
    messages: ClassVar[list] = []

    def poll(self, timeout):
        return DlqConsumer.messages.pop(0) if DlqConsumer.messages else None

    def commit(self, message, asynchronous):
        pass


class DlqProducer:
    instances: ClassVar[list["DlqProducer"]] = []

    def __init__(self, bootstrap, client_id, env=None):
        self.env, self.sent = env, []
        DlqProducer.instances.append(self)

    def send_raw(self, topic, key, value, headers=None):
        self.sent.append(topic)

    def flush(self, timeout=10.0):
        pass


def test_dlq_replay_reads_and_replays_within_one_environment(monkeypatch):
    monkeypatch.setattr(dlq, "Consumer", DlqConsumer)
    monkeypatch.setattr(dlq, "EventProducer", DlqProducer)
    DlqConsumer.messages = [
        DlqMessage(b"docs.received"),
        DlqMessage(b"dev.orders.extracted"),  # a prefixed header is not prefixed twice
    ]

    count = dlq.replay("unused:9092", False, 10, idle_seconds=0, env="dev")

    assert count == 2
    kafka_consumer = DlqConsumer.instances[-1]
    assert kafka_consumer.config["group.id"] == "dev.dlq-replay"
    assert kafka_consumer.subscribed == ["dev.orders.dlq"]
    producer = DlqProducer.instances[-1]
    assert producer.env == "dev"  # the producer prefixes the logical targets
    assert producer.sent == ["docs.received", "orders.extracted"]


def test_dlq_main_env_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        dlq,
        "replay",
        lambda bootstrap, dry_run, max_messages, env: seen.update(env=env) or 0,
    )
    dlq.main(["--dry-run", "--env", "test"])
    assert seen == {"env": "test"}
