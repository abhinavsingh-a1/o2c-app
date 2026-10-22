"""Appending to the hash-chained archive in S3, idempotently (ADR-014).

S3 objects cannot be appended to, so the day's object (<yyyy>/<mm>/<dd>.jsonl) is read
and written back with the new line. One archiver process owns the chain: a lock
serialises the Kafka consumer and the review poller.

Idempotency: the index table audit_archive (o2c database) holds the source id (event id
or review id) of every archived entry. An entry is written to S3 first and then to the
index; after a crash in between, the start-up reconciliation adds the entries of the
newest object that the index lacks, so a redelivered event is still skipped.
"""

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from o2c_common.metrics import AUDIT_RECORDS
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from audit_archiver.chain import (
    GENESIS,
    day_key,
    entries,
    last_entry,
    new_entry,
    source_id,
    to_line,
)

log = logging.getLogger(__name__)


class Storage(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
    def keys(self) -> list[str]: ...


class S3Storage:
    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType="application/x-ndjson"
        )

    def keys(self) -> list[str]:
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket
        ):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return sorted(k for k in keys if k.endswith(".jsonl"))


_metadata = MetaData()
audit_archive = Table(
    "audit_archive",
    _metadata,
    Column("source_id", String(80), primary_key=True),  # event:<uuid> / review:<id>
    Column("seq", BigInteger, nullable=False, unique=True),
    Column("review_id", BigInteger, index=True),
    Column("s3_key", String(40), nullable=False),
    Column("hash", String(64), nullable=False),
    Column("archived_at", DateTime(timezone=True), nullable=False),
)


class PostgresIndex:
    def __init__(self, engine: Engine):
        self.engine = engine
        _metadata.create_all(engine)

    def seen(self, sid: str) -> bool:
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    select(audit_archive.c.seq).where(audit_archive.c.source_id == sid)
                ).first()
                is not None
            )

    def add(self, sid: str, entry: dict, key: str) -> None:
        review = entry["record"].get("id") if sid.startswith("review:") else None
        with self.engine.begin() as conn:
            conn.execute(
                insert(audit_archive)
                .values(
                    source_id=sid,
                    seq=entry["seq"],
                    review_id=review,
                    s3_key=key,
                    hash=entry["hash"],
                    archived_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )

    def max_review_id(self) -> int:
        with self.engine.connect() as conn:
            return (
                conn.execute(select(func.max(audit_archive.c.review_id))).scalar() or 0
            )


def jsonable(value):
    """Rows and payloads as plain JSON values (for the canonical JSON)."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    return value


class Archive:
    def __init__(
        self,
        storage: Storage,
        index,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.storage, self.index, self.clock = storage, index, clock
        self._lock = threading.Lock()
        self.head = (0, GENESIS)  # (seq, hash) of the newest entry

    def start(self) -> None:
        """Load the chain head from the newest object and reconcile the index with it."""
        keys = self.storage.keys()
        if not keys:
            return
        content = self.storage.get(keys[-1]) or b""
        newest = last_entry(content)
        if newest is None:
            return
        self.head = (newest["seq"], newest["hash"])
        for _, _, line in entries(keys[-1], content):
            entry = json.loads(line)
            sid = source_id(entry["source"], entry["record"])
            if not self.index.seen(sid):
                log.warning("index lacked %s (seq %s): added", sid, entry["seq"])
                self.index.add(sid, entry, keys[-1])

    def append(
        self, source: str, record: dict, idempotency_key: str | None
    ) -> dict | None:
        """Archive one record; None if it was archived before (idempotent)."""
        record = jsonable(record)
        sid = source_id(source, record)
        with self._lock:
            if self.index.seen(sid):
                return None
            seq, prev_hash = self.head
            now = self.clock()
            entry = new_entry(
                seq=seq + 1,
                prev_hash=prev_hash,
                timestamp=now,
                source=source,
                idempotency_key=idempotency_key,
                record=record,
            )
            key = day_key(now)
            self.storage.put(key, (self.storage.get(key) or b"") + to_line(entry))
            self.index.add(sid, entry, key)
            self.head = (entry["seq"], entry["hash"])
        AUDIT_RECORDS.labels(source).inc()
        return entry
