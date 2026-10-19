"""Seed 12 months of completed sales orders (2025-10-01 to 2026-09-30) into vbak/vbap.

Each customer regularly buys a fixed set of 6-10 materials, each with its own
typical quantity (mean and spread). About 2% of lines are outliers: a quantity
5-20x the usual, or a material the customer never buys. Outliers are written
to poc/data/history_outliers.json as {vbeln, posnr, reason}.

Deterministic: every customer gets its own seeded random generator, and orders
use fixed keys (vbeln H<kunnr[-4:]><n>, idempotency key hist-<kunnr>-<n>), so a
second run inserts nothing. Run seed.py first.

CLI: uv run python -m erp_mock.seed_history
"""

import json
import random
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp_mock.models import Customer, Material, Price, SalesOrder, SalesOrderItem

SEED = 1234
START = date(2025, 10, 1)
MONTHS = 12
OUTLIER_RATE = 0.02
KEY_PREFIX = "hist-"
DEFAULT_OUTLIERS_PATH = (
    Path(__file__).resolve().parents[4] / "poc" / "data" / "history_outliers.json"
)

# Typical order quantity range per unit; each customer-material mean is drawn from it.
MEAN_RANGE = {
    "PC": (5, 40),
    "PAK": (5, 30),
    "BOX": (2, 15),
    "ROL": (5, 40),
    "M": (20, 200),
    "KG": (50, 300),
    "L": (20, 200),
}


def months() -> list[date]:
    return [
        date(
            START.year + (START.month - 1 + i) // 12, (START.month - 1 + i) % 12 + 1, 1
        )
        for i in range(MONTHS)
    ]


def days_in_month(first: date) -> int:
    nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return (nxt - first).days


def list_prices(session: Session) -> dict[str, Decimal]:
    """matnr -> list price (kunnr NULL); the latest validity period wins."""
    prices = session.scalars(
        select(Price).where(Price.kunnr.is_(None)).order_by(Price.datab)
    ).all()
    return {p.matnr: p.kbetr for p in prices}


def quantity(rng: random.Random, mean: float, spread: float) -> Decimal:
    return Decimal(max(1, round(rng.gauss(mean, spread))))


def customer_orders(
    rng: random.Random,
    kunnr: str,
    materials: dict[str, Material],
    prices: dict[str, Decimal],
) -> tuple[list[SalesOrder], list[dict]]:
    """All history orders for one customer, plus the outliers among their lines."""
    regular = sorted(rng.sample(sorted(materials), rng.randint(6, 10)))
    never = sorted(set(materials) - set(regular))
    profile = {}
    for matnr in regular:
        low, high = MEAN_RANGE.get(materials[matnr].meins, (5, 40))
        mean = rng.uniform(low, high)
        profile[matnr] = (mean, mean * rng.uniform(0.15, 0.35))

    orders, outliers, n = [], [], 0
    for first in months():
        days = sorted(
            rng.randint(1, days_in_month(first)) for _ in range(rng.randint(4, 8))
        )
        for day in days:
            n += 1
            bstdk = first.replace(day=day)
            vbeln = f"H{kunnr[-4:]}{n:05d}"
            items = []
            for i, matnr in enumerate(rng.sample(regular, rng.randint(1, 6)), start=1):
                posnr = 10 * i
                mean, spread = profile[matnr]
                kwmeng = quantity(rng, mean, spread)
                if rng.random() < OUTLIER_RATE:
                    if rng.random() < 0.5:
                        kwmeng = Decimal(round(mean * rng.uniform(5, 20)))
                        reason = f"quantity {kwmeng} is {kwmeng / Decimal(round(mean)):.1f}x the usual {round(mean)}"
                    else:
                        matnr = rng.choice(
                            [m for m in never if m not in {it.matnr for it in items}]
                        )
                        kwmeng = Decimal(rng.randint(1, 20))
                        reason = f"material {matnr} is never bought by customer {kunnr}"
                    outliers.append({"vbeln": vbeln, "posnr": posnr, "reason": reason})
                netpr = prices[matnr]
                items.append(
                    SalesOrderItem(
                        posnr=posnr,
                        matnr=matnr,
                        arktx=materials[matnr].maktx,
                        kwmeng=kwmeng,
                        vrkme=materials[matnr].meins,
                        netpr=netpr,
                        netwr=(kwmeng * netpr).quantize(Decimal("0.01"), ROUND_HALF_UP),
                    )
                )
            orders.append(
                SalesOrder(
                    vbeln=vbeln,
                    kunnr=kunnr,
                    bstnk=f"PO-{bstdk:%y%m}-{n:04d}",
                    bstdk=bstdk,
                    erdat=datetime.combine(bstdk, time()),
                    waerk="EUR",
                    netwr=sum((it.netwr for it in items), Decimal("0.00")),
                    status="COMPLETED",
                    idempotency_key=f"{KEY_PREFIX}{kunnr}-{n}",
                    items=items,
                )
            )
    return orders, outliers


def history_counts(session: Session) -> tuple[int, int]:
    orders = session.scalar(
        select(func.count())
        .select_from(SalesOrder)
        .where(SalesOrder.idempotency_key.startswith(KEY_PREFIX))
    )
    lines = session.scalar(
        select(func.count())
        .select_from(SalesOrderItem)
        .join(SalesOrder)
        .where(SalesOrder.idempotency_key.startswith(KEY_PREFIX))
    )
    return orders, lines


def seed_history(
    session: Session, outliers_path: Path = DEFAULT_OUTLIERS_PATH
) -> dict[str, int]:
    """Insert missing history orders; returns created and total counts."""
    materials = {m.matnr: m for m in session.scalars(select(Material))}
    prices = list_prices(session)
    missing = sorted(set(materials) - set(prices))
    if missing:
        raise ValueError(
            f"No list price for materials {missing}; run erp_mock.seed first"
        )
    existing = set(
        session.scalars(
            select(SalesOrder.idempotency_key).where(
                SalesOrder.idempotency_key.startswith(KEY_PREFIX)
            )
        )
    )

    created_orders = created_lines = 0
    all_outliers = []
    for kunnr in session.scalars(select(Customer.kunnr).order_by(Customer.kunnr)):
        # One generator per customer: adding a customer doesn't change the others' history.
        orders, outliers = customer_orders(
            random.Random(f"{SEED}-{kunnr}"), kunnr, materials, prices
        )
        all_outliers += outliers
        for order in orders:
            if order.idempotency_key not in existing:
                session.add(order)
                created_orders += 1
                created_lines += len(order.items)
    session.commit()

    outliers_path = Path(outliers_path)
    outliers_path.parent.mkdir(parents=True, exist_ok=True)
    outliers_path.write_text(
        json.dumps(all_outliers, indent=2) + "\n", encoding="utf-8"
    )

    orders, lines = history_counts(session)
    return {
        "orders_created": created_orders,
        "lines_created": created_lines,
        "orders": orders,
        "lines": lines,
        "outliers": len(all_outliers),
    }


def main() -> None:
    from erp_mock.db import SessionLocal  # needs ERP_DATABASE_URL, so import only here

    with SessionLocal() as session:
        counts = seed_history(session)
    print(f"History orders: {counts['orders']} ({counts['orders_created']} created)")
    print(f"History lines:  {counts['lines']} ({counts['lines_created']} created)")
    print(
        f"Outliers:       {counts['outliers']} ({counts['outliers'] / counts['lines']:.1%} of lines)"
    )
    print(f"Outliers written to {DEFAULT_OUTLIERS_PATH}")


if __name__ == "__main__":
    main()
