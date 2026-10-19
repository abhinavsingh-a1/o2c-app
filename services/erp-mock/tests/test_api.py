from datetime import date
from decimal import Decimal

import pytest
from erp_mock.api import app, get_session
from erp_mock.models import Base, Price
from erp_mock.seed import DEFAULT_PATH, seed
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ON = "2026-06-15"  # inside the seeded list-price validity, independent of today


@pytest.fixture
def session_factory():
    # StaticPool: every session shares the one in-memory database.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        seed(session, DEFAULT_PATH)
    return factory


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def order_payload(**changes) -> dict:
    payload = {
        "kunnr": "1000101",
        "bstnk": "4500012345",
        "bstdk": "2026-10-15",
        "waerk": "EUR",
        "idempotency_key": "test-key-0000000001",
        "items": [
            {
                "matnr": "400101",
                "arktx": "Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk",
                "kwmeng": "10",
                "vrkme": "PAK",
                "netpr": "14.90",
            },
            {
                "matnr": "400104",
                "arktx": "Rillenkugellager 6205-2RS C3",
                "kwmeng": "4",
                "vrkme": "PC",
                "netpr": "8.75",
            },
        ],
    }
    return payload | changes


# --- customers --------------------------------------------------------------


def test_get_customer_found(client):
    response = client.get("/customers/1000104")
    assert response.status_code == 200
    assert response.json() == {
        "kunnr": "1000104",
        "name1": "Wienerwald Anlagenbau GmbH",
        "stras": "Brunner Straße 88",
        "pstlz": "1230",
        "ort01": "Wien",
        "land1": "AT",
        "waers": "EUR",
    }


def test_get_customer_not_found(client):
    response = client.get("/customers/9999999")
    assert response.status_code == 404


# --- materials --------------------------------------------------------------


def test_search_materials(client):
    response = client.get("/materials", params={"q": "din"})
    assert response.status_code == 200
    # case-insensitive, sorted by material number
    assert [m["matnr"] for m in response.json()] == [
        "400101",
        "400102",
        "400103",
        "400116",
    ]


def test_search_materials_no_match(client):
    response = client.get("/materials", params={"q": "does-not-exist"})
    assert response.status_code == 200
    assert response.json() == []


# --- prices -----------------------------------------------------------------


def test_price_lookup_returns_list_price(client):
    response = client.get("/prices", params={"matnr": "400101", "on": ON})
    assert response.status_code == 200
    body = response.json()
    assert (body["kunnr"], Decimal(body["kbetr"]), body["source"]) == (
        None,
        Decimal("14.90"),
        "list",
    )


def test_price_lookup_prefers_customer_price(client, session_factory):
    with session_factory() as session:
        session.add(
            Price(
                matnr="400101",
                kunnr="1000101",
                kbetr=Decimal("13.50"),
                datab=date(2026, 1, 1),
                datbi=date(2026, 12, 31),
            )
        )
        session.commit()

    response = client.get(
        "/prices", params={"matnr": "400101", "kunnr": "1000101", "on": ON}
    )
    body = response.json()
    assert (body["kunnr"], Decimal(body["kbetr"]), body["source"]) == (
        "1000101",
        Decimal("13.50"),
        "customer",
    )

    # another customer still gets the list price
    response = client.get(
        "/prices", params={"matnr": "400101", "kunnr": "1000102", "on": ON}
    )
    assert (response.json()["source"], Decimal(response.json()["kbetr"])) == (
        "list",
        Decimal("14.90"),
    )


def test_price_lookup_outside_validity_is_404(client):
    response = client.get("/prices", params={"matnr": "400101", "on": "2027-01-01"})
    assert response.status_code == 404


# --- sales orders -----------------------------------------------------------


def test_create_order_returns_201(client):
    response = client.post("/salesorders", json=order_payload())
    assert response.status_code == 201
    body = response.json()
    assert len(body["vbeln"]) == 10
    assert body["status"] == "OPEN"
    assert [item["posnr"] for item in body["items"]] == [10, 20]
    assert Decimal(body["netwr"]) == Decimal("149.00") + Decimal("35.00")


def test_same_idempotency_key_returns_200_with_same_vbeln(client):
    first = client.post("/salesorders", json=order_payload())
    second = client.post("/salesorders", json=order_payload())
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["vbeln"] == first.json()["vbeln"]


def test_unknown_customer_returns_422(client):
    response = client.post("/salesorders", json=order_payload(kunnr="9999999"))
    assert response.status_code == 422
    assert "unknown customer" in response.json()["detail"]


def test_unknown_material_returns_422(client):
    payload = order_payload()
    payload["items"][1]["matnr"] = "999999"
    response = client.post("/salesorders", json=payload)
    assert response.status_code == 422
    assert "unknown material" in response.json()["detail"]


def test_cancel_sets_status_cancelled(client):
    vbeln = client.post("/salesorders", json=order_payload()).json()["vbeln"]

    response = client.post(f"/salesorders/{vbeln}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert client.get(f"/salesorders/{vbeln}").json()["status"] == "CANCELLED"


def test_cancel_unknown_order_is_404(client):
    response = client.post("/salesorders/0000000000/cancel")
    assert response.status_code == 404
