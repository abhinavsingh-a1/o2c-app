import json
from datetime import UTC, datetime, timedelta

import verify_audit
from audit_archiver.chain import GENESIS, day_key, new_entry, to_line


class FakeStorage:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = objects or {}

    def get(self, key):
        return self.objects.get(key)

    def keys(self):
        return sorted(self.objects)


def chain(n: int, start=datetime(2026, 10, 22, 23, 58, tzinfo=UTC)) -> FakeStorage:
    """n entries, one per minute, so the chain spans two days."""
    storage, prev = FakeStorage(), GENESIS
    for i in range(n):
        at = start + timedelta(minutes=i)
        entry = new_entry(
            seq=i + 1,
            prev_hash=prev,
            timestamp=at,
            source="orders.posted",
            idempotency_key="k" * 64,
            record={"event_id": f"e{i}", "n": i},
        )
        key = day_key(at)
        storage.objects[key] = storage.objects.get(key, b"") + to_line(entry)
        prev = entry["hash"]
    return storage


def test_an_intact_chain_verifies(capsys):
    storage = chain(4)

    assert verify_audit.main([], storage=storage) == 0

    out = capsys.readouterr().out
    assert "chain intact: 4 entries in 2 objects" in out
    assert "2026/10/22.jsonl .. 2026/10/23.jsonl" in out
    assert "head seq 4" in out


def test_a_changed_record_is_reported_at_its_position(capsys):
    storage = chain(4)
    key = "2026/10/23.jsonl"
    lines = storage.objects[key].decode().splitlines()
    entry = json.loads(lines[0])  # seq 3
    entry["record"]["n"] = 999
    lines[0] = json.dumps(entry)
    storage.objects[key] = ("\n".join(lines) + "\n").encode()

    assert verify_audit.main([], storage=storage) == 1

    out = capsys.readouterr().out
    assert "BROKEN after 2 valid entries" in out
    assert "2026/10/23.jsonl line 1 (expected seq 3)" in out


def test_a_deleted_record_is_reported_where_it_is_missing(capsys):
    storage = chain(4)
    key = "2026/10/22.jsonl"
    first, _second = storage.objects[key].decode().splitlines()
    storage.objects[key] = (first + "\n").encode()  # seq 2 deleted

    assert verify_audit.main([], storage=storage) == 1

    assert "2026/10/23.jsonl line 1 (expected seq 2): seq is 3" in (
        capsys.readouterr().out
    )


def test_an_empty_archive():
    assert verify_audit.run(FakeStorage()) == (0, "archive is empty")
