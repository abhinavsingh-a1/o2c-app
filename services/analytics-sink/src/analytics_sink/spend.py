"""Incremental copy of LiteLLM's spend logs into raw.llm_spend (ADR-013).

Source: "LiteLLM_SpendLogs" in the litellm database (one row per LLM call: request id,
start time, spend, model, the virtual key's alias in metadata). The watermark is
LiteLLM's insert time (created_at), not the request's start time: LiteLLM writes spend
logs in batches, so a row can arrive after rows of later requests. Each run reads from
the newest copied created_at minus OVERLAP, so rows committed slightly out of order are
not missed; ON CONFLICT (request_id) DO NOTHING drops the ones already copied.
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from o2c_common.metrics import SPEND_COPY_RUNS, SPEND_ROWS_COPIED
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

OVERLAP = timedelta(minutes=5)
BATCH = 1000
EPOCH = datetime(1970, 1, 1)
SPEND_QUERY = text(
    """
    SELECT
        request_id,
        "startTime" AS start_time,
        spend,
        model,
        model_group,
        metadata ->> 'user_api_key_alias' AS key_alias,
        created_at
    FROM "LiteLLM_SpendLogs"
    WHERE created_at >= :since
    ORDER BY created_at, request_id
    LIMIT :limit OFFSET :offset
    """
)

# (since, limit, offset) -> rows as dicts
FetchSpend = Callable[[datetime, int, int], list[dict]]


def litellm_fetcher(engine: Engine) -> FetchSpend:
    def fetch(since: datetime, limit: int, offset: int) -> list[dict]:
        with engine.connect() as conn:
            result = conn.execute(
                SPEND_QUERY, {"since": since, "limit": limit, "offset": offset}
            )
            return [dict(row._mapping) for row in result]

    return fetch


def spend_row(source: dict) -> dict:
    start = source["start_time"]
    return {
        "request_id": source["request_id"],
        # LiteLLM stores UTC without a time zone
        "start_time": start.replace(tzinfo=UTC) if start.tzinfo is None else start,
        "spend_usd": float(source["spend"] or 0.0),
        "model": source["model"] or "",
        "model_group": source.get("model_group") or None,
        "key_alias": source.get("key_alias") or None,
        "source_created_at": source["created_at"],
        "ingested_at": datetime.now(UTC),
    }


class SpendCopier:
    def __init__(self, fetch: FetchSpend, store, batch: int = BATCH):
        self.fetch, self.store, self.batch = fetch, store, batch

    def run_once(self) -> int:
        """Copy every spend row since the watermark; the number of rows read."""
        newest = self.store.max_spend_created_at()
        since = newest - OVERLAP if newest else EPOCH
        read = 0
        while True:
            rows = self.fetch(since, self.batch, read)
            self.store.insert_spend([spend_row(r) for r in rows])
            read += len(rows)
            if len(rows) < self.batch:
                return read


def run_periodically(
    copier: SpendCopier, interval: float, stop: threading.Event
) -> None:
    """Run the copy every `interval` seconds until `stop` is set; a failed run is
    logged and retried at the next interval (the watermark makes it catch up)."""
    while not stop.is_set():
        try:
            rows = copier.run_once()
            SPEND_ROWS_COPIED.inc(rows)
            SPEND_COPY_RUNS.labels("ok").inc()
        except Exception:
            SPEND_COPY_RUNS.labels("error").inc()
            log.exception("copying LiteLLM spend logs failed")
        stop.wait(interval)


def make_copier(litellm_database_url: str, store) -> SpendCopier:
    engine = create_engine(litellm_database_url, pool_pre_ping=True)
    return SpendCopier(litellm_fetcher(engine), store)
