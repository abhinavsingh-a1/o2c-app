import argparse
import os
import time
from datetime import UTC, datetime

from confluent_kafka import Consumer, Message

from o2c_common.kafka import DLQ_TOPIC, EventProducer
from o2c_common.kafka_config import client_config
from o2c_common.topics import ENV_VAR, ENVIRONMENTS, environment, logical, physical


def read_headers(msg: Message) -> dict[str, str]:
    return {key: (value or b"").decode() for key, value in (msg.headers() or [])}


def replay(
    bootstrap: str,
    dry_run: bool,
    max_messages: int,
    idle_seconds: float = 5.0,
    env: str | None = None,
) -> int:
    """Replays the DLQ of one environment (`env`, else $O2C_ENV, else unprefixed) into
    the same environment's original topics."""
    env = environment(env)
    consumer = Consumer(
        client_config(
            bootstrap,
            {
                "group.id": physical("dlq-replay", env),
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            },
        )
    )
    consumer.subscribe([physical(DLQ_TOPIC, env)])
    producer = EventProducer(bootstrap, "dlq-replay", env=env)
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
            # Logical name (EventConsumer); older or foreign headers may carry the prefix
            target = logical(headers.get("original_topic") or "", env)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay messages from the dead-letter queue."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only list, change nothing"
    )
    parser.add_argument(
        "--max", type=int, default=1000, help="maximum messages to handle"
    )
    parser.add_argument(
        "--env", choices=ENVIRONMENTS, help=f"environment prefix (default: ${ENV_VAR})"
    )
    args = parser.parse_args(argv)
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
    count = replay(bootstrap, args.dry_run, args.max, env=args.env)
    print(f"{count} message(s) {'listed' if args.dry_run else 'replayed'}")


if __name__ == "__main__":
    main()
