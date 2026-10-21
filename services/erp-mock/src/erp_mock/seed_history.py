"""Seed 12 months of completed sales orders (2025-10-01 to 2026-09-30) into vbak/vbap.

The history is sampled from the customer buying profiles derived from the generated
purchase orders (poc/data/customer_profiles.json, see poc/derive_customer_profiles.py),
so history and POs describe the same buying behaviour: each customer orders the
materials it orders in the POs, as often as it does there, and quantities are drawn
from the quantities actually ordered for that material. About 2% of lines are outliers:
a quantity 5-20x the material's typical (median) quantity, or - once per material at
most, with a normal quantity - a material the customer never orders. Outliers are
written to poc/data/history_outliers.json as {vbeln, posnr, reason}.

Deterministic: every customer gets its own seeded random generator, and orders use fixed
keys (vbeln H<kunnr[-4:]><n>, idempotency key hist-<kunnr>-<n>), so a second run inserts
nothing. --reset first deletes the generated history orders (idempotency key "hist-..."),
e.g. after the profiles changed; other orders are never touched. Run seed.py first.

CLI: uv run python -m erp_mock.seed_history [--reset]
"""

import argparse
import json
import random
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from erp_mock.models import Customer, Material, Price, SalesOrder, SalesOrderItem

SEED = 1234
START = date(2025, 10, 1)
MONTHS = 12
OUTLIER_RATE = 0.02
MAX_LINES = 6
KEY_PREFIX = "hist-"
REPO_DATA = Path(__file__).resolve().parents[4] / "poc" / "data"
DEFAULT_OUTLIERS_PATH = REPO_DATA / "history_outliers.json"
DEFAULT_PROFILES_PATH = REPO_DATA / "customer_profiles.json"


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


def load_profiles(path: Path = DEFAULT_PROFILES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def weighted_sample(rng: random.Random, weights: dict[str, int], k: int) -> list[str]:
    """k different keys, each drawn with probability proportional to its weight."""
    remaining = dict(weights)
    chosen = []
    for _ in range(min(k, len(remaining))):
        key = rng.choices(list(remaining), weights=list(remaining.values()))[0]
        chosen.append(key)
        del remaining[key]
    return chosen


def typical_quantity(profile: dict) -> Decimal:
    return Decimal(str(profile["median"]))


def customer_orders(
    rng: random.Random,
    kunnr: str,
    customer_profile: dict,
    material_profiles: dict,
    materials: dict[str, Material],
    prices: dict[str, Decimal],
) -> tuple[list[SalesOrder], list[dict]]:
    """All history orders for one customer, plus the outliers among their lines."""
    ordered = {
        m: n for m, n in sorted(customer_profile["materials"].items()) if m in materials
    }
    never = sorted(set(materials) - set(ordered))

    orders, outliers, n = [], [], 0
    used_never: set[str] = set()  # each never-ordered material is an outlier only once
    for first in months():
        days = sorted(
            rng.randint(1, days_in_month(first)) for _ in range(rng.randint(4, 8))
        )
        for day in days:
            n += 1
            bstdk = first.replace(day=day)
            vbeln = f"H{kunnr[-4:]}{n:05d}"
            items = []
            picked = weighted_sample(rng, ordered, rng.randint(1, MAX_LINES))
            for i, matnr in enumerate(picked, start=1):
                posnr = 10 * i
                # Bootstrap: a quantity this material was really ordered in.
                kwmeng = Decimal(
                    str(rng.choice(material_profiles[matnr]["quantities"]))
                )
                if rng.random() < OUTLIER_RATE:
                    unused = [m for m in never if m not in used_never]
                    if unused and rng.random() < 0.5:
                        matnr = rng.choice(unused)
                        used_never.add(matnr)
                        # A normal amount of that material: the material is the anomaly.
                        kwmeng = Decimal(
                            str(rng.choice(material_profiles[matnr]["quantities"]))
                        )
                        reason = f"material {matnr} is never bought by customer {kunnr}"
                    else:
                        usual = typical_quantity(material_profiles[matnr])
                        kwmeng = Decimal(
                            round(usual * Decimal(str(rng.uniform(5, 20))))
                        )
                        reason = f"quantity {kwmeng} is {kwmeng / usual:.1f}x the usual {usual}"
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


def delete_history(session: Session) -> int:
    """Delete only the generated history orders (idempotency key "hist-..."); returns
    the number of orders deleted."""
    history = select(SalesOrder.vbeln).where(
        SalesOrder.idempotency_key.startswith(KEY_PREFIX)
    )
    session.execute(delete(SalesOrderItem).where(SalesOrderItem.vbeln.in_(history)))
    deleted = session.execute(
        delete(SalesOrder).where(SalesOrder.idempotency_key.startswith(KEY_PREFIX))
    ).rowcount
    session.commit()
    return deleted


def seed_history(
    session: Session,
    outliers_path: Path = DEFAULT_OUTLIERS_PATH,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    reset: bool = False,
) -> dict[str, int]:
    """Insert missing history orders; returns deleted, created and total counts."""
    profiles = load_profiles(profiles_path)
    materials = {m.matnr: m for m in session.scalars(select(Material))}
    prices = list_prices(session)
    missing = sorted(set(materials) - set(prices))
    if missing:
        raise ValueError(
            f"No list price for materials {missing}; run erp_mock.seed first"
        )
    customers = list(session.scalars(select(Customer.kunnr).order_by(Customer.kunnr)))
    unprofiled = sorted(set(customers) - set(profiles["customers"]))
    if unprofiled:
        raise ValueError(
            f"No buying profile for customers {unprofiled} in {profiles_path}"
        )

    deleted = delete_history(session) if reset else 0
    existing = set(
        session.scalars(
            select(SalesOrder.idempotency_key).where(
                SalesOrder.idempotency_key.startswith(KEY_PREFIX)
            )
        )
    )

    created_orders = created_lines = 0
    all_outliers = []
    for kunnr in customers:
        # One generator per customer: adding a customer doesn't change the others' history.
        orders, outliers = customer_orders(
            random.Random(f"{SEED}-{kunnr}"),
            kunnr,
            profiles["customers"][kunnr],
            profiles["materials"],
            materials,
            prices,
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
        "orders_deleted": deleted,
        "orders_created": created_orders,
        "lines_created": created_lines,
        "orders": orders,
        "lines": lines,
        "outliers": len(all_outliers),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed the ERP order history.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help='delete the generated history orders ("hist-" keys) first',
    )
    args = parser.parse_args(argv)
    from erp_mock.db import SessionLocal  # needs ERP_DATABASE_URL, so import only here

    with SessionLocal() as session:
        counts = seed_history(session, reset=args.reset)
    if args.reset:
        print(f"Deleted {counts['orders_deleted']} history orders")
    print(f"History orders: {counts['orders']} ({counts['orders_created']} created)")
    print(f"History lines:  {counts['lines']} ({counts['lines_created']} created)")
    print(
        f"Outliers:       {counts['outliers']} ({counts['outliers'] / counts['lines']:.1%} of lines)"
    )
    print(f"Outliers written to {DEFAULT_OUTLIERS_PATH}")


if __name__ == "__main__":
    main()
