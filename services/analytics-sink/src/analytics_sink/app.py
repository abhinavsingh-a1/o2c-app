"""Analytics sink: Kafka events, the DLQ and LiteLLM spend logs -> raw.* (ADR-013).

- EventConsumer, group "analytics-sink", on the pipeline topics: one row per event in
  raw.events (event_id primary key; the consumer's processed-events store and
  ON CONFLICT DO NOTHING make redeliveries harmless).
- RawMessageConsumer, group "analytics-sink-dlq", on orders.dlq: the raw value and
  headers of every DLQ message in raw.dlq (key: topic, partition, offset).
- Every SPEND_INTERVAL seconds: new rows of LiteLLM's spend logs -> raw.llm_spend.
- At start: schemas raw and analytics, the raw tables, and the read-only role
  grafana_ro (SELECT on analytics only).
GET /health (503 if a worker thread died) and GET /metrics on HEALTH_PORT.
"""

import logging
import threading
from collections.abc import Callable

from o2c_common.events import Envelope
from o2c_common.kafka import (
    DLQ_TOPIC,
    EventConsumer,
    EventProducer,
    PostgresStore,
    RawMessageConsumer,
)
from o2c_common.metrics import start_health_server
from pydantic_settings import BaseSettings, SettingsConfigDict

from analytics_sink.db import Store, dlq_row, event_row, setup
from analytics_sink.spend import make_copier, run_periodically

log = logging.getLogger("analytics-sink")

GROUP = "analytics-sink"
DLQ_GROUP = "analytics-sink-dlq"
TOPICS = [
    "docs.received",
    "docs.classified",
    "orders.extracted",
    "orders.validated",
    "orders.posted",
    "orders.exceptions",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    kafka_bootstrap: str = "localhost:9092"
    events_database_url: str
    # The litellm database (LiteLLM_SpendLogs); empty: no spend copy
    litellm_database_url: str = ""
    grafana_db_password: str = ""
    health_port: int = 8310
    spend_interval_seconds: float = 60.0


def make_event_handler(store) -> Callable[[Envelope], None]:
    def handle(event: Envelope) -> None:
        store.insert_event(event_row(event))

    return handle


def make_dlq_handler(store) -> Callable:
    def handle(msg) -> None:
        store.insert_dlq(dlq_row(msg))

    return handle


def start_workers(settings: Settings, store: Store) -> list[threading.Thread]:
    # The producer is only used if an event fails permanently (to the DLQ).
    producer = EventProducer(settings.kafka_bootstrap, GROUP)
    workers = [
        EventConsumer(
            bootstrap=settings.kafka_bootstrap,
            group_id=GROUP,
            topics=TOPICS,
            handler=make_event_handler(store),
            producer=producer,
            store=PostgresStore(settings.events_database_url),
        ).run,
        RawMessageConsumer(
            bootstrap=settings.kafka_bootstrap,
            group_id=DLQ_GROUP,
            topics=[DLQ_TOPIC],
            handler=make_dlq_handler(store),
        ).run,
    ]
    if settings.litellm_database_url:
        copier = make_copier(settings.litellm_database_url, store)
        stop = threading.Event()
        workers.append(
            lambda: run_periodically(copier, settings.spend_interval_seconds, stop)
        )
    else:
        log.warning("LITELLM_DATABASE_URL not set: LiteLLM spend logs are not copied")
    threads = [
        threading.Thread(target=work, name=f"worker-{i}", daemon=True)
        for i, work in enumerate(workers)
    ]
    for thread in threads:
        thread.start()
    return threads


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings()
    store = Store.from_url(settings.events_database_url)
    setup(store.engine, settings.grafana_db_password)
    threads = start_workers(settings, store)
    start_health_server(
        settings.health_port, lambda: all(t.is_alive() for t in threads)
    )
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
