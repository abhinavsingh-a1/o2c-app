import json
from datetime import date
from decimal import Decimal

from derive_customer_profiles import OUT, SOURCES, derive_profiles, load_orders, number
from generate_pos import DATA_DIR
from schema import LineItem, PurchaseOrder

MASTER = json.loads((DATA_DIR / "master_data.json").read_text(encoding="utf-8"))


def po(customer: str, lines: list[tuple[str, str]]) -> PurchaseOrder:
    return PurchaseOrder(
        po_number="X",
        po_date=date(2026, 1, 5),
        customer_name=customer,
        customer_number=None,
        currency="EUR",
        delivery_date=None,
        ship_to_address="Somewhere 1, 1010 Wien, Austria",
        lines=[
            LineItem(
                line_no=i,
                material_number=None,
                description=description,
                quantity=Decimal(qty),
                unit="PC",
                unit_price=Decimal("1.00"),
                line_total=Decimal(qty),
            )
            for i, (description, qty) in enumerate(lines, start=1)
        ],
        total_amount=Decimal(sum(Decimal(q) for _, q in lines)),
    )


def test_derive_profiles_counts_lines_and_pools_quantities():
    screws = "Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk"  # 400101
    nuts = "Sechskantmutter DIN 934 M8 verzinkt, 100 Stk"  # 400102
    orders = [
        po("Steirische Metallbau GmbH", [(screws, "10"), (nuts, "4")]),
        po("Steirische Metallbau GmbH", [(screws, "30")]),
        po("Donautal Maschinenbau AG", [(screws, "20.5")]),
    ]

    profiles = derive_profiles(orders, MASTER, ["test"])

    assert profiles["purchase_orders"] == 3
    assert profiles["customers"] == {
        "1000101": {"purchase_orders": 2, "materials": {"400101": 2, "400102": 1}},
        "1000102": {"purchase_orders": 1, "materials": {"400101": 1}},
    }
    screws_profile = profiles["materials"]["400101"]
    assert screws_profile["quantities"] == [10, 20.5, 30]  # pooled over customers
    assert screws_profile["median"] == 20.5
    assert screws_profile["unit"] == "PAK"
    assert (
        profiles["materials"]["400102"]["p10"]
        == profiles["materials"]["400102"]["p90"]
        == 4
    )


def test_number_keeps_json_readable():
    assert number(Decimal(12)) == 12 and isinstance(number(Decimal(12)), int)
    assert number(Decimal("12.5")) == 12.5


def test_committed_profiles_match_the_ground_truth():
    """poc/data/customer_profiles.json must be regenerated when the POs change."""
    root = DATA_DIR.parents[1]
    expected = derive_profiles(
        load_orders(SOURCES), MASTER, [s.relative_to(root).as_posix() for s in SOURCES]
    )
    assert json.loads(OUT.read_text(encoding="utf-8")) == expected
    assert expected["purchase_orders"] == 150
