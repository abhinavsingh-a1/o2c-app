import os

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

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


def create_topics(bootstrap: str, replication: int = 1) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    new_topics = [
        NewTopic(
            name,
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


if __name__ == "__main__":
    create_topics(os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"))
