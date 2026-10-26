"""KAFKA_ADDRESS_FAMILY -> broker.address.family in every Kafka client."""

import pytest
from o2c_common import dlq, kafka, topics
from o2c_common.kafka import EventConsumer, EventProducer, InMemoryStore
from o2c_common.kafka_config import ADDRESS_FAMILY_VAR, address_family, client_config


@pytest.fixture(autouse=True)
def no_family(monkeypatch):
    monkeypatch.delenv(ADDRESS_FAMILY_VAR, raising=False)
    monkeypatch.delenv("O2C_ENV", raising=False)


def test_default_is_any():
    assert address_family() == "any"


@pytest.mark.parametrize("value", ["any", "v4", "v6", " V4 "])
def test_known_values(monkeypatch, value):
    monkeypatch.setenv(ADDRESS_FAMILY_VAR, value)
    assert address_family() == value.strip().lower()


def test_empty_means_any(monkeypatch):
    monkeypatch.setenv(ADDRESS_FAMILY_VAR, "")
    assert address_family() == "any"


def test_unknown_value_is_rejected(monkeypatch):
    monkeypatch.setenv(ADDRESS_FAMILY_VAR, "ipv4")
    with pytest.raises(ValueError, match="any, v4, v6"):
        address_family()


def test_client_config_maps_the_family_and_keeps_settings(monkeypatch):
    monkeypatch.setenv(ADDRESS_FAMILY_VAR, "v4")
    assert client_config("kafka:9092", {"group.id": "g"}) == {
        "bootstrap.servers": "kafka:9092",
        "broker.address.family": "v4",
        "group.id": "g",
    }


class Recorder:
    """Stands in for Producer, Consumer and AdminClient; records the config."""

    configs: list[dict] = []  # noqa: RUF012 - shared on purpose, reset per test

    def __init__(self, config):
        Recorder.configs.append(config)

    def subscribe(self, topics):
        pass

    def poll(self, timeout):
        return None

    def close(self):
        pass

    def create_topics(self, new_topics):
        return {}


@pytest.fixture
def recorder(monkeypatch):
    Recorder.configs = []
    monkeypatch.setenv(ADDRESS_FAMILY_VAR, "v4")
    return Recorder


def test_producer(monkeypatch, recorder):
    monkeypatch.setattr(kafka, "Producer", recorder)
    EventProducer("kafka:9092", "intake")
    [config] = recorder.configs
    assert config["broker.address.family"] == "v4"
    assert config["enable.idempotence"] is True


def test_consumer(monkeypatch, recorder):
    monkeypatch.setattr(kafka, "Consumer", recorder)
    EventConsumer(
        bootstrap="kafka:9092",
        group_id="classifier",
        topics=["docs.received"],
        handler=lambda e: None,
        producer=None,
        store=InMemoryStore(),
    ).run(max_messages=0)
    [config] = recorder.configs
    assert config["broker.address.family"] == "v4"
    assert config["group.id"] == "classifier"


def test_admin(monkeypatch, recorder):
    monkeypatch.setattr(topics, "AdminClient", recorder)
    topics.create_topics("kafka:9092")
    [config] = recorder.configs
    assert config == {"bootstrap.servers": "kafka:9092", "broker.address.family": "v4"}


def test_dlq_tool(monkeypatch, recorder):
    monkeypatch.setattr(dlq, "Consumer", recorder)
    monkeypatch.setattr(kafka, "Producer", recorder)
    dlq.replay("kafka:9092", dry_run=True, max_messages=1, idle_seconds=0)
    assert [c["broker.address.family"] for c in recorder.configs] == ["v4", "v4"]
