from o2c_common.dlq import read_headers


class FakeMessage:
    def __init__(self, headers: list[tuple[str, bytes | None]] | None):
        self._headers = headers

    def headers(self) -> list[tuple[str, bytes | None]] | None:
        return self._headers


def test_normal_headers_are_decoded():
    msg = FakeMessage(
        [
            ("original_topic", b"docs.received"),
            ("original_offset", b"42"),
            ("error", "Straße nicht gefunden".encode()),
        ]
    )
    assert read_headers(msg) == {
        "original_topic": "docs.received",
        "original_offset": "42",
        "error": "Straße nicht gefunden",
    }


def test_no_headers_gives_empty_dict():
    # confluent-kafka returns None when a message has no headers
    assert read_headers(FakeMessage(None)) == {}


def test_header_with_none_value_becomes_empty_string():
    msg = FakeMessage([("original_topic", b"docs.received"), ("error", None)])
    assert read_headers(msg) == {"original_topic": "docs.received", "error": ""}
