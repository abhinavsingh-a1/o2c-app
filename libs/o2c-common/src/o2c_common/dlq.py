import argparse
import os
import time
from datetime import UTC, datetime

from confluent_kafka import Consumer, Message

from o2c_common.kafka import DLQ_TOPIC, EventProducer


def read_headers(msg: Message) -> dict[str, str]:
    return {key: (value or b"").decode() for key, value in (msg.headers() or [])}


def replay(
    bootstrap: str, dry_run: bool, max_messages: int, idle_seconds: float = 5.0
) -> int:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "dlq-replay",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([DLQ_TOPIC])
    producer = EventProducer(bootstrap, "dlq-replay")
    count, last_message = 0, time.monotonic()
    try:
        while count < max_messages:
            msg = consumer.poll(1.0)
            if msg is None:
                if time.monotonic() - last_message > idle_seconds:
                    break  # DLQ is empty
                continue
            last_message = time.monotonic()
            if msg.error():
                continue
            headers = read_headers(msg)
            target = headers.get("original_topic")
            print(
                f"{'WOULD REPLAY' if dry_run else 'REPLAY'} -> {target} | "
                f"failed_at={headers.get('failed_at')} | error={headers.get('error', '')[:80]}"
            )
            count += 1
            if dry_run or not target:
                continue
            producer.send_raw(
                target,
                msg.key(),
                msg.value(),
                [
                    ("replayed_from_dlq", b"true"),
                    ("replayed_at", datetime.now(UTC).isoformat().encode()),
                ],
            )
            producer.flush()  # message is safely back before we move past it
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay messages from the dead-letter queue."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only list, change nothing"
    )
    parser.add_argument(
        "--max", type=int, default=1000, help="maximum messages to handle"
    )
    args = parser.parse_args()
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
    count = replay(bootstrap, args.dry_run, args.max)
    print(f"{count} message(s) {'listed' if args.dry_run else 'replayed'}")


if __name__ == "__main__":
    main()
