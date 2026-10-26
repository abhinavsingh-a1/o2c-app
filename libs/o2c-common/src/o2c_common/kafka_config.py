"""Connection settings shared by every Kafka client (producer, consumers, admin, DLQ tool).

KAFKA_ADDRESS_FAMILY (any, v4 or v6; default any) becomes librdkafka's
broker.address.family. In the kind cluster host.docker.internal also resolves to an IPv6
address that pods cannot reach ("Network is unreachable"), so the services there set v4
(o2c-gitops envs/*, ADR-017).
"""

import os

ADDRESS_FAMILY_VAR = "KAFKA_ADDRESS_FAMILY"
ADDRESS_FAMILIES = ("any", "v4", "v6")


def address_family() -> str:
    """$KAFKA_ADDRESS_FAMILY, default any. Rejects unknown values."""
    value = os.environ.get(ADDRESS_FAMILY_VAR, "").strip().lower() or "any"
    if value not in ADDRESS_FAMILIES:
        raise ValueError(
            f"{ADDRESS_FAMILY_VAR} must be one of {', '.join(ADDRESS_FAMILIES)}: {value!r}"
        )
    return value


def client_config(bootstrap: str, settings: dict | None = None) -> dict:
    """librdkafka configuration: bootstrap servers and address family, plus `settings`."""
    return {
        "bootstrap.servers": bootstrap,
        "broker.address.family": address_family(),
        **(settings or {}),
    }
