import pytest
from o2c_common.events import Envelope
from pydantic import ValidationError

KEY = "a" * 64


def make() -> Envelope:
    return Envelope.new("docs.received", "intake", KEY, {"s3_key": "po.pdf"})


def test_roundtrip_bytes():
    event = make()
    assert Envelope.from_bytes(event.to_bytes()) == event


def test_child_keeps_keys_and_sets_causation():
    first = make()
    second = first.child(
        "docs.classified", "classifier", {"doc_type": "purchase_order"}
    )
    assert second.idempotency_key == first.idempotency_key
    assert second.correlation_id == first.correlation_id
    assert second.causation_id == first.event_id
    assert second.event_id != first.event_id


def test_unknown_fields_are_ignored():
    data = make().model_dump(mode="json") | {"new_field": "from a newer service"}
    assert Envelope.model_validate(data).event_type == "docs.received"


def test_missing_payload_is_rejected():
    data = make().model_dump(mode="json")
    del data["payload"]
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_bad_event_type_is_rejected():
    with pytest.raises(ValidationError):
        Envelope.new("Docs Received!", "intake", KEY, {})


def test_events_are_immutable():
    with pytest.raises(ValidationError):
        make().producer = "someone else"


def test_occurred_at_is_utc():
    assert make().occurred_at.utcoffset().total_seconds() == 0


def test_child_id_is_deterministic():
    first = make()
    a = first.child("docs.classified", "classifier", {})
    b = first.child("docs.classified", "classifier", {})
    assert a.event_id == b.event_id
