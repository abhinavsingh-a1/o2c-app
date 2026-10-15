import hashlib


def idempotency_key(source: str, content: bytes) -> str:
    """Same document from the same source always gives the same key."""
    return hashlib.sha256(source.encode() + b":" + content).hexdigest()
