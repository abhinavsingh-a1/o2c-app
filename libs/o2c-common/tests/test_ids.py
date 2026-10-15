from o2c_common.ids import idempotency_key


def test_same_input_same_key():
    assert idempotency_key("email", b"po") == idempotency_key("email", b"po")


def test_different_content_different_key():
    assert idempotency_key("email", b"po1") != idempotency_key("email", b"po2")
