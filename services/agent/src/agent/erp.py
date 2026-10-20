from datetime import date
from typing import Any

import httpx


class ErpRejected(Exception):
    """The ERP refused the data (HTTP 422). Retrying will not help."""


class ErpClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self._http = client or httpx.Client(base_url=base_url, timeout=10)

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = self._http.get(path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()  # 5xx or connection problems -> exception -> retry
        return response.json()

    def get_customer(self, kunnr: str) -> dict | None:
        return self._get(f"/customers/{kunnr}")

    def search_customers(self, name: str) -> list[dict]:
        return self._get("/customers", {"name": name}) or []

    def get_material(self, matnr: str) -> dict | None:
        return self._get(f"/materials/{matnr}")

    def search_materials(self, q: str = "") -> list[dict]:
        return self._get("/materials", {"q": q}) or []

    def get_price(self, matnr: str, kunnr: str, on: date) -> dict | None:
        return self._get(
            "/prices", {"matnr": matnr, "kunnr": kunnr, "on": on.isoformat()}
        )

    def create_order(self, order: dict) -> dict:
        response = self._http.post("/salesorders", json=order)
        if response.status_code == 422:
            raise ErpRejected(response.json().get("detail"))
        response.raise_for_status()
        return response.json()

    def get_order(self, vbeln: str) -> dict | None:
        return self._get(f"/salesorders/{vbeln}")

    def cancel_order(self, vbeln: str) -> dict:
        response = self._http.post(f"/salesorders/{vbeln}/cancel")
        response.raise_for_status()
        return response.json()
