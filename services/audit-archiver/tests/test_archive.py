import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from audit_archiver.app import (
    REVIEW_SOURCE,
    archive_new_reviews,
    make_event_handler,
)
from audit_archiver.archive import Archive, jsonable
from audit_archiver.chain import (
    GENESIS,
    canonical_json,
    day_key,
    entry_hash,
    to_line,
    verify,
)
from o2c_common.events import Envelope

KEY = "k" * 64
DAY1 = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
BEFORE_MIDNIGHT = datetime(2026, 10, 22, 23, 59, 0, tzinfo=UTC)


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get(self, key):
        return self.objects.get(key)

    def put(self, key, data):
        self.objects[key] = data

    def keys(self):
        return sorted(self.objects)

    def walk(self):
        return [(k, self.objects[k]) for k in self.keys()]

    def lines(self, key) -> list[dict]:
        return [json.loads(line) for line in self.objects[key].decode().splitlines()]

    def rewrite(self, key, entries: list[dict]):
        self.objects[key] = b"".join(to_line(e) for e in entries)


class FakeIndex:
    def __init__(self):
        self.rows: dict[str, tuple[dict, str]] = {}

    def seen(self, sid):
        return sid in self.rows

    def add(self, sid, entry, key):
        self.rows.setdefault(sid, (entry, key))

    def max_review_id(self):
        ids = [int(s.split(":")[1]) for s in self.rows if s.startswith("review:")]
        return max(ids, default=0)


class Clock:
    def __init__(self, start=DAY1, step=timedelta(seconds=30)):
        self.now, self.step = start, step

    def __call__(self):
        current, self.now = self.now, self.now + self.step
        return current


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def archive(storage):
    return Archive(storage, FakeIndex(), Clock())


# Fixed events: Envelope.new gives a new event id on every call
EVENTS = [
    Envelope.new("orders.posted", "agent", KEY, {"po_number": f"PO-{n}", "n": n})
    for n in range(10)
]


def event(n: int) -> Envelope:
    return EVENTS[n]


def review(review_id: int) -> dict:
    return {
        "id": review_id,
        "idempotency_key": KEY,
        "action": "approve",
        "reviewer": "anna",
        "po_before": {"total_amount": "10.00"},
        "decided_at": datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
    }


def fill(archive, n: int = 5) -> None:
    handle = make_event_handler(archive)
    for i in range(n):
        handle(event(i))


# --- the chain ---


def test_hash_is_sha256_of_prev_hash_and_the_canonical_entry():
    entry = {
        "seq": 1,
        "timestamp": "t",
        "source": "s",
        "idempotency_key": "k",
        "record": {"b": 1, "a": "ä"},
        "prev_hash": GENESIS,
    }
    expected = hashlib.sha256(
        (GENESIS + canonical_json(entry)).encode("utf-8")
    ).hexdigest()

    assert entry_hash(entry) == expected
    assert canonical_json({"b": 1, "a": "ä"}) == '{"a":"ä","b":1}'


def test_entries_form_a_chain_that_verifies(archive, storage):
    fill(archive, 5)

    entries = storage.lines(day_key(DAY1))
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5]
    assert entries[0]["prev_hash"] == GENESIS
    for previous, current in itertools.pairwise(entries):
        assert current["prev_hash"] == previous["hash"]
    assert verify(storage.walk()) == (5, None)
    assert set(entries[0]) == {
        "seq",
        "timestamp",
        "source",
        "idempotency_key",
        "record",
        "prev_hash",
        "hash",
    }
    assert entries[0]["source"] == "orders.posted"
    assert entries[0]["idempotency_key"] == KEY


def test_the_chain_continues_across_days(storage):
    archive = Archive(storage, FakeIndex(), Clock(start=BEFORE_MIDNIGHT))
    fill(archive, 4)  # 23:59:00, :30, 00:00:00 next day, :30

    day1, day2 = storage.keys()
    assert (day1, day2) == ("2026/10/22.jsonl", "2026/10/23.jsonl")
    first_of_day2 = storage.lines(day2)[0]
    assert first_of_day2["prev_hash"] == storage.lines(day1)[-1]["hash"]
    assert first_of_day2["seq"] == 3
    assert verify(storage.walk()) == (4, None)


def test_each_event_and_review_is_archived_once(archive, storage):
    handle = make_event_handler(archive)
    handle(event(1))
    handle(event(1))  # redelivered
    archive.append(REVIEW_SOURCE, review(7), KEY)
    archive.append(REVIEW_SOURCE, review(7), KEY)

    assert [e["seq"] for e in storage.lines(day_key(DAY1))] == [1, 2]


def test_restart_continues_the_chain_and_reconciles_the_index(storage):
    first = Archive(storage, FakeIndex(), Clock())
    fill(first, 3)

    # crash after the S3 write, before the index write: a fresh, empty index
    restarted = Archive(storage, FakeIndex(), Clock(start=DAY1 + timedelta(seconds=10)))
    restarted.start()
    make_event_handler(restarted)(event(2))  # redelivered: must be skipped
    make_event_handler(restarted)(event(9))

    entries = storage.lines(day_key(DAY1))
    assert [e["seq"] for e in entries] == [1, 2, 3, 4]
    assert verify(storage.walk()) == (4, None)


# --- tampering ---


@pytest.mark.parametrize("field", ["record", "timestamp", "source", "idempotency_key"])
def test_changing_one_entry_breaks_the_chain_at_that_entry(archive, storage, field):
    fill(archive, 5)
    key = day_key(DAY1)
    entries = storage.lines(key)
    entries[2][field] = {"forged": True} if field == "record" else "forged"
    storage.rewrite(key, entries)

    count, broken = verify(storage.walk())

    assert count == 2
    assert (broken.key, broken.line, broken.seq) == (key, 3, 3)
    assert "changed" in broken.reason


def test_rehashing_a_changed_entry_breaks_the_next_link(archive, storage):
    fill(archive, 5)
    key = day_key(DAY1)
    entries = storage.lines(key)
    entries[2]["record"]["po_number"] = "FORGED"
    entries[2]["hash"] = entry_hash(entries[2])  # a forger recomputes this hash
    storage.rewrite(key, entries)

    count, broken = verify(storage.walk())

    assert (count, broken.seq, broken.line) == (3, 4, 4)
    assert "prev_hash" in broken.reason


def test_deleting_one_entry_breaks_the_chain_where_it_was(archive, storage):
    fill(archive, 5)
    key = day_key(DAY1)
    entries = storage.lines(key)
    del entries[1]  # seq 2
    storage.rewrite(key, entries)

    count, broken = verify(storage.walk())

    assert (count, broken.seq, broken.line) == (1, 2, 2)
    assert "seq is 3" in broken.reason


def test_deleting_the_last_entry_of_a_day_is_found_in_the_next_day(storage):
    fill(Archive(storage, FakeIndex(), Clock(start=BEFORE_MIDNIGHT)), 4)
    day1, day2 = storage.keys()
    storage.rewrite(day1, storage.lines(day1)[:-1])

    count, broken = verify(storage.walk())

    assert (count, broken.key, broken.line, broken.seq) == (1, day2, 1, 2)


def test_a_damaged_line_is_reported(archive, storage):
    fill(archive, 2)
    key = day_key(DAY1)
    storage.objects[key] = storage.objects[key].replace(b'"seq":2', b'"seq":2,,')

    count, broken = verify(storage.walk())

    assert (count, broken.line, broken.reason) == (1, 2, "not valid JSON")


# --- review decisions ---


def test_new_review_decisions_are_archived_in_id_order(archive, storage):
    table = [review(i) for i in (3, 5, 8)]

    def fetch(after, limit):
        return [r for r in table if r["id"] > after][:limit]

    assert archive_new_reviews(archive, fetch, limit=2) == 3
    table.append(review(9))
    assert archive_new_reviews(archive, fetch, limit=2) == 1
    assert archive_new_reviews(archive, fetch, limit=2) == 0

    records = [e["record"] for e in storage.lines(day_key(DAY1))]
    assert [r["id"] for r in records] == [3, 5, 8, 9]
    assert records[0]["decided_at"] == "2026-10-22T10:00:00+00:00"


def test_jsonable_converts_database_values():
    value = {"amount": Decimal("10.50"), "at": DAY1, "items": ({"x": 1},)}

    assert jsonable(value) == {
        "amount": "10.50",
        "at": "2026-10-22T10:00:00+00:00",
        "items": [{"x": 1}],
    }
