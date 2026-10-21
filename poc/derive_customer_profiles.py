"""Derive customer buying profiles from the generated purchase orders.

Reads the ground truth of poc/data and ml/data/train and writes
poc/data/customer_profiles.json, which erp_mock.seed_history samples the ERP order
history from, so that history and POs describe the same buying behaviour:

  customers.<kunnr>.materials   material -> number of PO lines (the order frequency)
  materials.<matnr>.quantities  every ordered quantity of that material (sorted)

Quantities are pooled per material across customers: generate_pos.py draws a quantity
from the material's unit, independent of the customer, and per customer-material there
are only 2-5 lines - too few to describe a distribution. Price-deviation lines are kept;
only their price differs, not the quantity.

Run with: uv run --group poc python poc/derive_customer_profiles.py
Deterministic: the same ground truth always gives the same file.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median, quantiles

from generate_pos import DATA_DIR, write_json
from schema import PurchaseOrder

ROOT = DATA_DIR.parents[1]
SOURCES = [DATA_DIR / "ground_truth", ROOT / "ml" / "data" / "train" / "ground_truth"]
OUT = DATA_DIR / "customer_profiles.json"


def load_orders(sources: list[Path]) -> list[PurchaseOrder]:
    return [
        PurchaseOrder.model_validate_json(path.read_text(encoding="utf-8"))
        for source in sources
        for path in sorted(source.glob("PO-*.json"))
    ]


def number(value) -> float | int:
    """12.0 -> 12, 12.5 -> 12.5 (keeps the JSON readable)."""
    value = float(value)
    return int(value) if value.is_integer() else value


def derive_profiles(
    orders: list[PurchaseOrder], master: dict, sources: list[str]
) -> dict:
    customer_by_name = {c["name"]: c["customer_number"] for c in master["customers"]}
    material_by_text = {m["description"]: m for m in master["materials"]}

    pos_per_customer = Counter()
    lines = defaultdict(Counter)  # kunnr -> matnr -> lines
    quantities = defaultdict(list)  # matnr -> quantities
    for po in orders:
        kunnr = customer_by_name[po.customer_name]
        pos_per_customer[kunnr] += 1
        for line in po.lines:
            matnr = material_by_text[line.description]["material_number"]
            lines[kunnr][matnr] += 1
            quantities[matnr].append(number(line.quantity))

    units = {m["material_number"]: m["unit"] for m in master["materials"]}
    return {
        "sources": sources,
        "purchase_orders": len(orders),
        "customers": {
            kunnr: {
                "purchase_orders": pos_per_customer[kunnr],
                "materials": dict(sorted(lines[kunnr].items())),
            }
            for kunnr in sorted(lines)
        },
        "materials": {
            matnr: {
                "unit": units[matnr],
                "lines": len(qs),
                "median": number(median(qs)),
                "p10": number(round(quantiles(qs, n=10)[0], 1))
                if len(qs) > 1
                else number(qs[0]),
                "p90": number(round(quantiles(qs, n=10)[-1], 1))
                if len(qs) > 1
                else number(qs[0]),
                "quantities": sorted(qs),
            }
            for matnr, qs in sorted(quantities.items())
        },
    }


def main() -> None:
    master = json.loads((DATA_DIR / "master_data.json").read_text(encoding="utf-8"))
    orders = load_orders(SOURCES)
    profiles = derive_profiles(
        orders, master, [s.relative_to(ROOT).as_posix() for s in SOURCES]
    )
    write_json(OUT, profiles)
    customers = profiles["customers"]
    print(f"{len(orders)} POs -> {OUT}")
    for kunnr, c in customers.items():
        print(f"  {kunnr}: {c['purchase_orders']} POs, {len(c['materials'])} materials")


if __name__ == "__main__":
    main()
