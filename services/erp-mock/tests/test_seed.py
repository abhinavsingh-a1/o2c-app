import json
from datetime import date
from decimal import Decimal

import pytest
from erp_mock.models import Base, Customer, Price
from erp_mock.seed import DEFAULT_PATH, seed, split_address
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

EXPECTED = {"kna1": 8, "mara": 25, "prcd": 25}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_seed_creates_rows(session):
    assert seed(session, DEFAULT_PATH) == EXPECTED

    customer = session.get(Customer, "1000104")
    assert (
        customer.name1,
        customer.stras,
        customer.pstlz,
        customer.ort01,
        customer.land1,
        customer.waers,
    ) == (
        "Wienerwald Anlagenbau GmbH",
        "Brunner Straße 88",
        "1230",
        "Wien",
        "AT",
        "EUR",
    )
    price = session.scalars(select(Price).where(Price.matnr == "400101")).one()
    assert (price.kunnr, price.kbetr, price.datab, price.datbi) == (
        None,
        Decimal("14.90"),
        date(2026, 1, 1),
        date(2026, 12, 31),
    )


def test_seed_twice_gives_same_counts(session):
    assert seed(session, DEFAULT_PATH) == EXPECTED
    assert seed(session, DEFAULT_PATH) == EXPECTED


def test_seed_again_updates_changed_list_price(session, tmp_path):
    seed(session, DEFAULT_PATH)
    data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    data["materials"][0]["list_price"] = "15.50"
    changed = tmp_path / "master_data.json"
    changed.write_text(json.dumps(data), encoding="utf-8")

    assert seed(session, changed) == EXPECTED
    price = session.scalars(
        select(Price).where(Price.matnr == data["materials"][0]["material_number"])
    ).one()
    assert price.kbetr == Decimal("15.50")


def test_split_address():
    assert split_address("Am Kanal 19, 79108 Freiburg im Breisgau, Germany") == {
        "stras": "Am Kanal 19",
        "pstlz": "79108",
        "ort01": "Freiburg im Breisgau",
        "land1": "DE",
    }


def test_split_address_rejects_unknown_country():
    with pytest.raises(ValueError, match="Switzerland"):
        split_address("Bahnhofstrasse 1, 8001 Zürich, Switzerland")
