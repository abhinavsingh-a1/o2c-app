"""Seed the ERP mock with customers, materials and list prices.

Loads poc/data/master_data.json into kna1, mara and prcd. Safe to run repeatedly:
customers and materials are merged by primary key, and a list price is updated
in place when one already exists for the same material and validity period.

CLI: uv run python -m erp_mock.seed [path/to/master_data.json]
"""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_mock.models import Customer, Material, Price

DEFAULT_PATH = Path(__file__).resolve().parents[4] / "poc" / "data" / "master_data.json"
VALID_FROM = date(2026, 1, 1)
VALID_TO = date(2026, 12, 31)
COUNTRY_CODES = {"Austria": "AT", "Germany": "DE"}


def split_address(address: str) -> dict[str, str]:
    """'Brunner Straße 88, 1230 Wien, Austria' -> stras, pstlz, ort01, land1."""
    street, postal_city, country = address.split(", ")
    postal_code, city = postal_city.split(" ", 1)
    if country not in COUNTRY_CODES:
        raise ValueError(f"No ISO code for country {country!r} in address {address!r}")
    return {
        "stras": street,
        "pstlz": postal_code,
        "ort01": city,
        "land1": COUNTRY_CODES[country],
    }


def upsert_list_price(session: Session, matnr: str, kbetr: Decimal) -> None:
    price = session.scalars(
        select(Price).where(
            Price.matnr == matnr,
            Price.kunnr.is_(None),
            Price.datab == VALID_FROM,
            Price.datbi == VALID_TO,
        )
    ).one_or_none()
    if price is None:
        session.add(
            Price(
                matnr=matnr, kunnr=None, kbetr=kbetr, datab=VALID_FROM, datbi=VALID_TO
            )
        )
    else:
        price.kbetr = kbetr


def count_rows(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in (Customer, Material, Price)
    }


def seed(session: Session, path: Path = DEFAULT_PATH) -> dict[str, int]:
    """Load master data into kna1, mara and prcd; returns the row count per table."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    for c in data["customers"]:
        session.merge(
            Customer(
                kunnr=c["customer_number"],
                name1=c["name"],
                waers=c["currency"],
                **split_address(c["address"]),
            )
        )
    for m in data["materials"]:
        session.merge(
            Material(
                matnr=m["material_number"], maktx=m["description"], meins=m["unit"]
            )
        )
    session.flush()  # prices reference mara

    for m in data["materials"]:
        upsert_list_price(session, m["material_number"], Decimal(m["list_price"]))

    session.commit()
    return count_rows(session)


def main() -> None:
    from erp_mock.db import SessionLocal  # needs ERP_DATABASE_URL, so import only here

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    with SessionLocal() as session:
        counts = seed(session, path)
    for table, count in counts.items():
        print(f"{table}: {count} rows")


if __name__ == "__main__":
    main()
