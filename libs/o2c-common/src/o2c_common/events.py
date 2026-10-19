import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field


class Envelope(BaseModel):
    """Standard wrapper for every Kafka message."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(pattern=r"^[a-z]+\.[a-z_.]+$")
    schema_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    producer: str
    idempotency_key: str = Field(min_length=16, max_length=64)
    correlation_id: str
    causation_id: UUID | None = None
    payload: dict[str, Any]

    @classmethod
    def new(
        cls,
        event_type: str,
        producer: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> "Envelope":
        """First event of a document's journey."""
        return cls(
            event_type=event_type,
            producer=producer,
            idempotency_key=idempotency_key,
            correlation_id=idempotency_key,
            payload=payload,
        )

    def child(
        self, event_type: str, producer: str, payload: dict[str, Any]
    ) -> "Envelope":
        """Next event in the same journey. The ID is derived from the parent,
        so re-processing the same message produces the same child event."""
        return Envelope(
            event_id=uuid5(NAMESPACE_URL, f"{self.event_id}/{event_type}"),
            event_type=event_type,
            producer=producer,
            idempotency_key=self.idempotency_key,
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            payload=payload,
        )

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Envelope":
        return cls.model_validate_json(data)

    @property
    def key(self) -> bytes:
        return self.idempotency_key.encode()


if __name__ == "__main__":
    out = Path(__file__).parents[2] / "schemas" / "envelope.schema.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(Envelope.model_json_schema(), indent=2) + "\n")
    print(f"wrote {out}")
