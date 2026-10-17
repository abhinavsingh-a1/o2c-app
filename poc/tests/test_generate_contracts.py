import json
import re
from decimal import Decimal

from generate_contracts import generate, ordered_materials
from generate_pos import DATA_DIR

CONTRACTS = DATA_DIR / "contracts"
ANNEX_ROW = re.compile(r"^\| (\d{6}) \| (.+?) \| (\w+) \| ([\d.,]+) \|$", re.MULTILINE)
TOLERANCE = re.compile(r"(?:bis zu|of up to) (\d) ?%|Festpreise|are fixed")


def master() -> dict:
    return json.loads((DATA_DIR / "master_data.json").read_text(encoding="utf-8"))


def parse_price(text: str, german: bool) -> Decimal:
    if german:
        text = text.replace(".", "").replace(",", ".")
    return Decimal(text.replace(",", ""))


def test_one_contract_per_customer_and_two_amendments():
    numbers = sorted(c["customer_number"] for c in master()["customers"])
    assert sorted(p.stem for p in CONTRACTS.glob("???????.md")) == numbers
    amendments = sorted(CONTRACTS.glob("*-amendment-1.md"))
    assert len(amendments) == 2
    assert all(p.name.split("-")[0] in numbers for p in amendments)


def test_annex_a_prices_equal_list_price_and_cover_all_ordered_materials():
    data = master()
    materials = {m["material_number"]: m for m in data["materials"]}
    ordered = ordered_materials(DATA_DIR, data)
    for customer in data["customers"]:
        number = customer["customer_number"]
        text = (CONTRACTS / f"{number}.md").read_text(encoding="utf-8")
        german = text.startswith("# Rahmenliefervertrag")
        rows = ANNEX_ROW.findall(text)
        assert len(rows) >= 10, number
        for material_number, description, unit, price in rows:
            m = materials[material_number]
            assert (description, unit) == (m["description"], m["unit"])
            assert parse_price(price, german) == Decimal(m["list_price"]), (
                number,
                material_number,
            )
        assert ordered[number] <= {row[0] for row in rows}, number


def test_tolerances_vary_between_0_2_and_3_percent():
    found = set()
    for path in CONTRACTS.glob("???????.md"):
        match = TOLERANCE.search(path.read_text(encoding="utf-8"))
        found.add(int(match.group(1)) if match.group(1) else 0)
    assert found == {0, 2, 3}


def test_amendments_change_payment_terms_only():
    for path in CONTRACTS.glob("*-amendment-1.md"):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"Zahlungsbedingungen|Payment terms", text)
        assert not ANNEX_ROW.search(text), path.name


def test_generate_is_deterministic(tmp_path):
    generate(tmp_path / "a")
    generate(tmp_path / "b")
    files = sorted(p.name for p in (tmp_path / "a" / "contracts").iterdir())
    assert len(files) == 10
    for name in files:
        a = (tmp_path / "a" / "contracts" / name).read_bytes()
        assert a == (tmp_path / "b" / "contracts" / name).read_bytes(), name
