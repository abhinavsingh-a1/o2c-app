"""Verify the hash-chained audit archive (ADR-014).

Usage: uv run python ml/verify_audit.py [--bucket o2c-audit]

Reads every <yyyy>/<mm>/<dd>.jsonl object of the bucket in key (= time) order and walks
the chain from seq 1: each entry must have the next seq, the previous entry's hash as
prev_hash, and a hash that matches its content (audit_archiver.chain). Prints the number
of verified entries and the head, or the first broken entry (object, line, expected seq,
reason) and exits 1. A changed entry breaks at that entry; a deleted or inserted one at
the position where the sequence or the prev_hash link no longer fits.

S3 settings from the environment or .env: S3_ENDPOINT_URL (default
http://localhost:8333), S3_ACCESS_KEY, S3_SECRET_KEY.
"""

import argparse
import sys
from collections.abc import Iterator

from audit_archiver.archive import S3Storage
from audit_archiver.chain import last_entry, verify

BUCKET = "o2c-audit"


def objects(storage) -> Iterator[tuple[str, bytes]]:
    keys = storage.keys()  # Storage.keys(): the object keys in order, not a dict
    for key in keys:
        yield key, storage.get(key) or b""


def run(storage) -> tuple[int, str]:
    """(exit code, report)."""
    keys = storage.keys()
    count, broken = verify(objects(storage))
    if broken:
        return 1, f"BROKEN after {count} valid entries: {broken}"
    if not keys:
        return 0, "archive is empty"
    head = last_entry(storage.get(keys[-1]) or b"")
    return 0, (
        f"chain intact: {count} entries in {len(keys)} objects "
        f"({keys[0]} .. {keys[-1]}), head seq {head['seq']} hash {head['hash'][:16]}..."
    )


def main(argv: list[str] | None = None, storage=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", default=BUCKET)
    args = parser.parse_args(argv)
    if storage is None:
        import boto3
        from langfuse_common import setting

        client = boto3.client(
            "s3",
            endpoint_url=setting("S3_ENDPOINT_URL", "http://localhost:8333"),
            aws_access_key_id=setting("S3_ACCESS_KEY"),
            aws_secret_access_key=setting("S3_SECRET_KEY"),
        )
        storage = S3Storage(client, args.bucket)
    code, report = run(storage)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
