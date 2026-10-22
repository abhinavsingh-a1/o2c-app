"""Audit archiver: decisions -> hash-chained JSONL in s3://o2c-audit (ADR-014).

- EventConsumer, group "audit-archiver", on orders.validated, orders.posted and
  orders.exceptions: one entry per event (the whole Envelope as the record).
- Every REVIEW_POLL_SECONDS: new rows of public.review_decisions (id above the highest
  archived review id), one entry per row.
Entries go to <yyyy>/<mm>/<dd>.jsonl (UTC) in one chain across days; ml/verify_audit.py
checks it. GET /health and GET /metrics on HEALTH_PORT.
"""

import logging
import threading
from collections.abc import Callable

import boto3
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PostgresStore
from o2c_common.metrics import start_health_server
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text

from audit_archiver.archive import Archive, PostgresIndex, S3Storage

log = logging.getLogger("audit-archiver")

GROUP = "audit-archiver"
TOPICS = ["orders.validated", "orders.posted", "orders.exceptions"]
REVIEW_SOURCE = "review_decisions"
REVIEWS_QUERY = text(
    "SELECT * FROM public.review_decisions WHERE id > :after ORDER BY id LIMIT :limit"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    events_database_url: str
    audit_bucket: str = "o2c-audit"
    review_poll_seconds: float = 10.0
    health_port: int = 8320


def make_event_handler(archive: Archive) -> Callable[[Envelope], None]:
    def handle(event: Envelope) -> None:
        archive.append(
            event.event_type, event.model_dump(mode="json"), event.idempotency_key
        )

    return handle


# (after review id, limit) -> rows as dicts
FetchReviews = Callable[[int, int], list[dict]]


def review_fetcher(engine) -> FetchReviews:
    def fetch(after: int, limit: int) -> list[dict]:
        with engine.connect() as conn:
            rows = conn.execute(REVIEWS_QUERY, {"after": after, "limit": limit})
            return [dict(row._mapping) for row in rows]

    return fetch


def archive_new_reviews(archive: Archive, fetch: FetchReviews, limit: int = 100) -> int:
    """Append every review decision not archived yet, in id order; how many."""
    archived = 0
    while True:
        rows = fetch(archive.index.max_review_id(), limit)
        for row in rows:
            if archive.append(REVIEW_SOURCE, row, row.get("idempotency_key")):
                archived += 1
        if len(rows) < limit:
            return archived


def poll_reviews(
    archive: Archive, fetch: FetchReviews, interval: float, stop: threading.Event
) -> None:
    while not stop.is_set():
        try:
            archive_new_reviews(archive, fetch)
        except Exception:
            log.exception("archiving review decisions failed")
        stop.wait(interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings()
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    engine = create_engine(settings.events_database_url, pool_pre_ping=True)
    archive = Archive(S3Storage(s3, settings.audit_bucket), PostgresIndex(engine))
    archive.start()
    log.info("chain head: seq %s", archive.head[0])

    producer = EventProducer(settings.kafka_bootstrap, GROUP)
    consumer = EventConsumer(
        bootstrap=settings.kafka_bootstrap,
        group_id=GROUP,
        topics=TOPICS,
        handler=make_event_handler(archive),
        producer=producer,
        store=PostgresStore(settings.events_database_url),
    )
    stop = threading.Event()
    threads = [
        threading.Thread(target=consumer.run, name="consumer", daemon=True),
        threading.Thread(
            target=poll_reviews,
            args=(
                archive,
                review_fetcher(engine),
                settings.review_poll_seconds,
                stop,
            ),
            name="reviews",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    start_health_server(
        settings.health_port, lambda: all(t.is_alive() for t in threads)
    )
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
