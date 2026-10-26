"""Kafka topics, and the environment prefix of topics and consumer groups (ADR-017).

O2C_ENV (dev, test or prod) prefixes every topic and consumer group, so that several
environments can share one Kafka: dev.docs.received, group dev.classifier. Without
O2C_ENV (local Compose, tests) names stay as they are. Code always uses the logical
names (TOPICS, "classifier"); EventProducer, EventConsumer, the DLQ tool and this
script apply the prefix.

Usage: python -m o2c_common.topics [--env dev|test|prod] [--reset]
"""

import argparse
import os
import time

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

from o2c_common.kafka_config import client_config

ENV_VAR = "O2C_ENV"
ENVIRONMENTS = ("dev", "test", "prod")
DAY_MS = 24 * 60 * 60 * 1000
PARTITIONS = 3
TOPICS = {  # name: retention in days
    "docs.received": 7,
    "docs.classified": 7,
    "orders.extracted": 7,
    "orders.validated": 7,
    "orders.exceptions": 30,
    "orders.posted": 30,
    "orders.dlq": 30,
}


def environment(env: str | None = None) -> str | None:
    """`env`, else $O2C_ENV; None (no prefix) if neither is set. Rejects unknown names."""
    value = (env if env is not None else os.environ.get(ENV_VAR, "")).strip()
    if not value:
        return None
    if value not in ENVIRONMENTS:
        raise ValueError(
            f"{ENV_VAR} must be one of {', '.join(ENVIRONMENTS)}: {value!r}"
        )
    return value


def physical(name: str, env: str | None) -> str:
    """Kafka name of a logical topic or group: docs.received -> dev.docs.received."""
    return f"{env}.{name}" if env else name


def logical(name: str, env: str | None) -> str:
    """The inverse of physical(): dev.docs.received -> docs.received (for env dev)."""
    return name.removeprefix(f"{env}.") if env else name


def create_topics(bootstrap: str, replication: int = 1, env: str | None = None) -> None:
    admin = AdminClient(client_config(bootstrap))
    new_topics = [
        NewTopic(
            physical(name, env),
            num_partitions=PARTITIONS,
            replication_factor=replication,
            config={"retention.ms": str(days * DAY_MS)},
        )
        for name, days in TOPICS.items()
    ]
    for name, future in admin.create_topics(new_topics).items():
        try:
            future.result()
            print(f"created {name}")
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.TOPIC_ALREADY_EXISTS:
                print(f"exists  {name}")
            else:
                raise


def delete_topics(bootstrap: str, env: str | None = None) -> None:
    admin = AdminClient(client_config(bootstrap))
    names = [physical(name, env) for name in TOPICS]
    for name, future in admin.delete_topics(names).items():
        try:
            future.result()
            print(f"deleted {name}")
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                print(f"missing {name}")
            else:
                raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create (or reset) the Kafka topics.")
    parser.add_argument(
        "--env", choices=ENVIRONMENTS, help=f"topic prefix (default: ${ENV_VAR})"
    )
    parser.add_argument(
        "--reset", action="store_true", help="delete and recreate (localhost only)"
    )
    args = parser.parse_args(argv)
    env = environment(args.env)
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
    if args.reset:
        if "localhost" not in bootstrap:
            raise SystemExit("refusing to reset topics outside localhost")
        delete_topics(bootstrap, env)
        time.sleep(5)  # Kafka deletes topics in the background
    create_topics(bootstrap, env=env)


if __name__ == "__main__":
    main()
