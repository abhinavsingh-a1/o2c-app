import uuid

from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, InMemoryStore

BOOTSTRAP = "localhost:9092"
producer = EventProducer(BOOTSTRAP, "smoke")

event = Envelope.new("docs.received", "smoke", uuid.uuid4().hex, {"hello": "kafka"})
producer.send("docs.received", event)
producer.flush()
print("sent    ", event.event_id)


def fail(e: Envelope) -> None:
    raise RuntimeError("simulated failure")


EventConsumer(
    bootstrap=BOOTSTRAP,
    group_id=f"smoke-{uuid.uuid4().hex[:6]}",
    topics=["docs.received"],
    handler=fail,
    producer=producer,
    store=InMemoryStore(),
    base_delay=0.5,
).run(max_messages=1)
print("handler failed 3 times -> message sent to orders.dlq")
