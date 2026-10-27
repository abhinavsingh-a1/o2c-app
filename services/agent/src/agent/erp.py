import random
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
from o2c_common.metrics import ERP_CALLS, ERP_RETRIES, init_labels

# Transient failures of an idempotent read: worth another attempt within the same call.
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
ATTEMPTS = 3
BACKOFF_BASE = 0.2  # seconds; waits ~0.2 s and ~0.4 s, plus up to 100% jitter


class ErpRejected(Exception):
    """The ERP refused the data (HTTP 422). Retrying will not help."""


def backoff(attempt: int, base: float = BACKOFF_BASE) -> float:
    """Wait before retry number `attempt` (1, 2, ...): exponential with full jitter on top."""
    delay = base * 2 ** (attempt - 1)
    return delay + random.uniform(0, delay)


class ErpClient:
    """Reads (GET) retry transient errors here; creates and cancels (POST) never do: a
    POST that timed out may have been applied, so the graph's saga handles those."""

    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = client or httpx.Client(base_url=base_url, timeout=10)
        self._sleep = sleep
        init_labels(ERP_RETRIES, ERP_CALLS)

    def _get(self, call: str, path: str, params: dict | None = None) -> Any:
        for attempt in range(1, ATTEMPTS + 1):
            last = attempt == ATTEMPTS
            try:
                response = self._http.get(path, params=params)
            except RETRY_EXCEPTIONS:
                if last:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or last:
                    break
            ERP_RETRIES.labels(call).inc()
            self._sleep(backoff(attempt))
        if response.status_code == 404:
            return None
        response.raise_for_status()  # other 5xx -> exception -> the consumer retries
        return response.json()

    def get_customer(self, kunnr: str) -> dict | None:
        return self._get("get_customer", f"/customers/{kunnr}")

    def search_customers(self, name: str) -> list[dict]:
        return self._get("search_customers", "/customers", {"name": name}) or []

    def get_material(self, matnr: str) -> dict | None:
        return self._get("get_material", f"/materials/{matnr}")

    def search_materials(self, q: str = "") -> list[dict]:
        return self._get("search_materials", "/materials", {"q": q}) or []

    def get_price(self, matnr: str, kunnr: str, on: date) -> dict | None:
        return self._get(
            "get_price",
            "/prices",
            {"matnr": matnr, "kunnr": kunnr, "on": on.isoformat()},
        )

    def create_order(self, order: dict) -> dict:
        response = self._http.post("/salesorders", json=order)
        if response.status_code == 422:
            raise ErpRejected(response.json().get("detail"))
        response.raise_for_status()
        return response.json()

    def get_order(self, vbeln: str) -> dict | None:
        return self._get("get_order", f"/salesorders/{vbeln}")

    def cancel_order(self, vbeln: str) -> dict:
        response = self._http.post(f"/salesorders/{vbeln}/cancel")
        response.raise_for_status()
        return response.json()
