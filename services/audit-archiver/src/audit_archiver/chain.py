"""The hash chain of the audit archive (ADR-014). Pure functions, no I/O.

Every archived decision is one JSON line:
    {"seq", "timestamp", "source", "idempotency_key", "record", "prev_hash", "hash"}
- seq counts from 1 without gaps over the whole archive (across days);
- prev_hash is the previous entry's hash (GENESIS for seq 1);
- hash = sha256(prev_hash + canonical JSON of the entry without "hash"), hex.
  The hash covers every field, not only "record": otherwise seq, timestamp, source or
  idempotency_key could be changed without breaking the chain.
Changing, inserting or deleting any entry changes a hash or breaks a prev_hash link
from that position on; verify() reports the first such position.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

GENESIS = "0" * 64
FIELDS = ("seq", "timestamp", "source", "idempotency_key", "record", "prev_hash")


def canonical_json(value) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 characters kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(entry: dict) -> str:
    content = {field: entry[field] for field in FIELDS}
    return hashlib.sha256(
        (entry["prev_hash"] + canonical_json(content)).encode("utf-8")
    ).hexdigest()


def new_entry(
    *,
    seq: int,
    prev_hash: str,
    timestamp: datetime,
    source: str,
    idempotency_key: str | None,
    record: dict,
) -> dict:
    entry = {
        "seq": seq,
        "timestamp": timestamp.isoformat(),
        "source": source,
        "idempotency_key": idempotency_key,
        "record": record,
        "prev_hash": prev_hash,
    }
    entry["hash"] = entry_hash(entry)
    return entry


def to_line(entry: dict) -> bytes:
    return (canonical_json(entry) + "\n").encode("utf-8")


def day_key(timestamp: datetime) -> str:
    """s3://o2c-audit/<yyyy>/<mm>/<dd>.jsonl (UTC); keys sort in time order."""
    return f"{timestamp:%Y/%m/%d}.jsonl"


def source_id(source: str, record: dict) -> str:
    """What makes an entry unique: the event id or the review decision id."""
    if source == "review_decisions":
        return f"review:{record['id']}"
    return f"event:{record['event_id']}"


@dataclass(frozen=True)
class Break:
    """The first entry where the chain does not hold."""

    key: str  # object (day file)
    line: int  # 1-based line in that object
    seq: int | None  # the seq expected at this position
    reason: str

    def __str__(self) -> str:
        return f"{self.key} line {self.line} (expected seq {self.seq}): {self.reason}"


def entries(key: str, content: bytes) -> Iterator[tuple[str, int, str]]:
    for number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if line.strip():
            yield key, number, line


def verify(objects: Iterable[tuple[str, bytes]]) -> tuple[int, Break | None]:
    """Walk the whole chain (objects in key order). Returns (entries verified, the first
    break or None)."""
    prev_hash, expected_seq, count = GENESIS, 1, 0
    for key, content in objects:
        for key_, number, line in entries(key, content):
            where = (key_, number, expected_seq)
            try:
                entry = json.loads(line)
                missing = [f for f in (*FIELDS, "hash") if f not in entry]
            except json.JSONDecodeError:
                return count, Break(*where, "not valid JSON")
            if missing:
                return count, Break(*where, f"fields missing: {', '.join(missing)}")
            if entry["seq"] != expected_seq:
                return count, Break(
                    *where, f"seq is {entry['seq']} (a record is missing or extra)"
                )
            if entry["prev_hash"] != prev_hash:
                return count, Break(
                    *where, "prev_hash does not match the previous hash"
                )
            if entry_hash(entry) != entry["hash"]:
                return count, Break(*where, "hash does not match the content (changed)")
            prev_hash, expected_seq, count = entry["hash"], expected_seq + 1, count + 1
    return count, None


def last_entry(content: bytes) -> dict | None:
    lines = [line for line in content.decode("utf-8").splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else None
