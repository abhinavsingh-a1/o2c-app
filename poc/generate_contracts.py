"""Generate framework contracts (Markdown) for the 8 PoC customers.

Writes into poc/data/contracts/:
  <customer_number>.md              framework contract with Annex A (agreed prices)
  <customer_number>-amendment-1.md  for 2 customers: new payment terms, prices unchanged

Annex A lists every material the customer ordered in any PO (topped up with
random extra materials to at least 10 rows) at exactly its list_price, so the
price deviations in labels.json stay the only deviations.

Needs master_data.json and ground_truth/ from generate_pos.py.
Run with: uv run --group poc python poc/generate_contracts.py
No network or LLM calls; the output is identical on every run.
"""

import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from generate_pos import DATA_DIR, SUPPLIER_ADDRESS, SUPPLIER_NAME, fmt_date, fmt_number
from schema import PurchaseOrder

SEED = 2026
MIN_ANNEX_ROWS = 10
NUM_AMENDMENTS = 2
VALID_FROM = date(2026, 1, 1)
VALID_TO = date(2027, 12, 31)
# Each value appears at least twice across the 8 customers.
TOLERANCES = [0, 0, 2, 2, 2, 3, 3, 3]

PAYMENT_TERMS = {
    "de": [
        "30 Tage netto ab Rechnungsdatum.",
        "14 Tage abzüglich 2 % Skonto oder 30 Tage netto ab Rechnungsdatum.",
        "45 Tage netto ab Rechnungsdatum.",
        "60 Tage netto ab Rechnungsdatum.",
    ],
    "en": [
        "30 days net from invoice date.",
        "2% discount if paid within 14 days, otherwise 30 days net from invoice date.",
        "45 days net from invoice date.",
        "60 days net from invoice date.",
    ],
}

DELIVERY_TERMS = {
    "de": [
        (
            "DAP Lieferanschrift des Kunden (Incoterms 2020). Ab einem Bestellwert von "
            "EUR 500,00 netto frachtfrei, darunter Frachtpauschale EUR 25,00."
        ),
        "FCA Wels (Incoterms 2020). Der Kunde beauftragt die Spedition.",
        "DDP Lieferanschrift des Kunden (Incoterms 2020), frei Haus.",
    ],
    "en": [
        (
            "DAP customer's delivery address (Incoterms 2020). Free freight for orders "
            "of EUR 500.00 net or more, otherwise a flat freight charge of EUR 25.00."
        ),
        "FCA Wels (Incoterms 2020). The customer appoints the carrier.",
        "DDP customer's delivery address (Incoterms 2020), carriage paid.",
    ],
}

LEAD_TIME = {
    "de": "Lieferzeit: {days} Arbeitstage ab Auftragsbestätigung.",
    "en": "Lead time: {days} working days from order confirmation.",
}

# German documents use German formats, English ones English formats.
FORMATS = {"de": ("dmy", "de"), "en": ("text", "en")}


def ordered_materials(data_dir: Path, master: dict) -> dict[str, set[str]]:
    """customer_number -> material numbers that appear in that customer's POs.

    Uses names and descriptions, because the ground truth leaves customer_number
    and material_number null where the PDF does not print them.
    """
    by_name = {c["name"]: c["customer_number"] for c in master["customers"]}
    by_description = {
        m["description"]: m["material_number"] for m in master["materials"]
    }
    ordered: dict[str, set[str]] = {
        c["customer_number"]: set() for c in master["customers"]
    }
    for path in sorted((data_dir / "ground_truth").glob("PO-*.json")):
        po = PurchaseOrder.model_validate_json(path.read_text(encoding="utf-8"))
        ordered[by_name[po.customer_name]].update(
            by_description[line.description] for line in po.lines
        )
    return ordered


def build_contracts(
    rng: random.Random, master: dict, ordered: dict[str, set[str]]
) -> list[dict]:
    materials = {m["material_number"]: m for m in master["materials"]}
    tolerances = TOLERANCES[:]
    rng.shuffle(tolerances)
    amended = set(
        rng.sample([c["customer_number"] for c in master["customers"]], NUM_AMENDMENTS)
    )

    contracts = []
    for customer, tolerance in zip(master["customers"], tolerances, strict=True):
        number = customer["customer_number"]
        lang = rng.choice(["de", "en"])
        annex = set(ordered[number])
        others = sorted(set(materials) - annex)
        annex |= set(rng.sample(others, max(0, MIN_ANNEX_ROWS - len(annex))))
        payment = rng.randrange(len(PAYMENT_TERMS[lang]))
        contract = {
            "customer": customer,
            "lang": lang,
            "contract_no": f"RV-2026-{number}",
            "signed": date(2025, 12, rng.randint(1, 19)),
            "tolerance": tolerance,
            "payment": PAYMENT_TERMS[lang][payment],
            "delivery": DELIVERY_TERMS[lang][rng.randrange(len(DELIVERY_TERMS[lang]))],
            "lead_time": LEAD_TIME[lang].format(days=rng.choice([3, 5, 10])),
            "annex": [materials[n] for n in sorted(annex)],
            "amendment": None,
        }
        if number in amended:
            new_payment = rng.choice(
                [i for i in range(len(PAYMENT_TERMS[lang])) if i != payment]
            )
            contract["amendment"] = {
                "effective": date(2026, rng.choice([4, 7]), 1),
                "payment": PAYMENT_TERMS[lang][new_payment],
            }
        contracts.append(contract)
    return contracts


def tolerance_clause(lang: str, tolerance: int) -> str:
    if lang == "de":
        if tolerance == 0:
            return (
                "Die in Anlage A vereinbarten Preise sind Festpreise. Jede Abweichung des "
                "Bestellpreises vom vereinbarten Preis bedarf der schriftlichen Zustimmung beider Parteien."
            )
        return (
            f"Abweichungen des Bestellpreises vom in Anlage A vereinbarten Preis von bis zu {tolerance} % "
            "je Position werden ohne gesonderte Zustimmung akzeptiert. Größere Abweichungen "
            "bedürfen der schriftlichen Zustimmung beider Parteien."
        )
    if tolerance == 0:
        return (
            "The prices agreed in Annex A are fixed. Any deviation of an order price from the "
            "agreed price requires the written approval of both parties."
        )
    return (
        f"Deviations of an order price from the price agreed in Annex A of up to {tolerance}% "
        "per line item are accepted without further approval. Larger deviations require the "
        "written approval of both parties."
    )


def render_contract(c: dict) -> str:
    customer, lang = c["customer"], c["lang"]
    df, nf = FORMATS[lang]
    start, end = fmt_date(VALID_FROM, df), fmt_date(VALID_TO, df)
    rows = [
        f"| {m['material_number']} | {m['description']} | {m['unit']} | {fmt_number(Decimal(m['list_price']), nf)} |"
        for m in c["annex"]
    ]
    if lang == "de":
        head = [
            "# Rahmenliefervertrag",
            "",
            f"Vertragsnummer: {c['contract_no']}",
            "",
            "## 1. Vertragsparteien",
            "",
            f"**Lieferant:** {SUPPLIER_NAME}, {SUPPLIER_ADDRESS}",
            "",
            f"**Kunde:** {customer['name']}, {customer['address']} (Kundennummer {customer['customer_number']})",
            "",
            "## 2. Laufzeit",
            "",
            f"Dieser Vertrag gilt vom {start} bis {end}. Er gilt für alle Bestellungen des Kunden in diesem Zeitraum.",
            "",
            "## 3. Zahlungsbedingungen",
            "",
            f"{c['payment']} Alle Preise verstehen sich in {customer['currency']} zuzüglich gesetzlicher Umsatzsteuer.",
            "",
            "## 4. Lieferbedingungen",
            "",
            f"{c['delivery']} {c['lead_time']}",
            "",
            "## 5. Preistoleranz",
            "",
            tolerance_clause(lang, c["tolerance"]),
            "",
            "## 6. Unterschriften",
            "",
            f"Wels, {fmt_date(c['signed'], df)}",
            "",
            f"{SUPPLIER_NAME} — Geschäftsführung | {customer['name']} — Einkaufsleitung",
            "",
            "## Anlage A – Vereinbarte Preise",
            "",
            f"Preise in {customer['currency']} je Mengeneinheit, netto.",
            "",
            "| Artikelnr. | Bezeichnung | ME | Vereinbarter Preis |",
        ]
    else:
        head = [
            "# Framework Supply Agreement",
            "",
            f"Contract number: {c['contract_no']}",
            "",
            "## 1. Parties",
            "",
            f"**Supplier:** {SUPPLIER_NAME}, {SUPPLIER_ADDRESS}",
            "",
            f"**Customer:** {customer['name']}, {customer['address']} (customer number {customer['customer_number']})",
            "",
            "## 2. Term",
            "",
            f"This agreement is valid from {start} to {end}. It applies to all orders the customer places in this period.",
            "",
            "## 3. Payment terms",
            "",
            f"{c['payment']} All prices are in {customer['currency']}, excluding VAT.",
            "",
            "## 4. Delivery terms",
            "",
            f"{c['delivery']} {c['lead_time']}",
            "",
            "## 5. Price tolerance",
            "",
            tolerance_clause(lang, c["tolerance"]),
            "",
            "## 6. Signatures",
            "",
            f"Wels, {fmt_date(c['signed'], df)}",
            "",
            f"{SUPPLIER_NAME} — Managing Director | {customer['name']} — Head of Purchasing",
            "",
            "## Annex A – Agreed prices",
            "",
            f"Prices in {customer['currency']} per unit, net.",
            "",
            "| Material No. | Description | Unit | Agreed unit price |",
        ]
    return "\n".join([*head, "|---|---|---|---:|", *rows]) + "\n"


def render_amendment(c: dict) -> str:
    customer, lang, amendment = c["customer"], c["lang"], c["amendment"]
    df = FORMATS[lang][0]
    effective = fmt_date(amendment["effective"], df)
    if lang == "de":
        lines = [
            f"# 1. Nachtrag zum Rahmenliefervertrag {c['contract_no']}",
            "",
            f"zwischen {SUPPLIER_NAME} (Lieferant) und {customer['name']} (Kunde, Kundennummer {customer['customer_number']})",
            "",
            "## Änderung der Zahlungsbedingungen",
            "",
            f"Mit Wirkung ab {effective} erhält Abschnitt 3 (Zahlungsbedingungen) folgende Fassung:",
            "",
            f"> {amendment['payment']}",
            "",
            "## Übrige Bestimmungen",
            "",
            (
                "Alle übrigen Bestimmungen des Vertrags, insbesondere die Preise in Anlage A und die "
                "Preistoleranz, bleiben unverändert."
            ),
        ]
    else:
        lines = [
            f"# Amendment No. 1 to Framework Supply Agreement {c['contract_no']}",
            "",
            f"between {SUPPLIER_NAME} (Supplier) and {customer['name']} (Customer, customer number {customer['customer_number']})",
            "",
            "## Change of payment terms",
            "",
            f"With effect from {effective}, section 3 (Payment terms) is replaced by:",
            "",
            f"> {amendment['payment']}",
            "",
            "## Other provisions",
            "",
            (
                "All other provisions of the agreement, in particular the prices in Annex A and the "
                "price tolerance, remain unchanged."
            ),
        ]
    return "\n".join(lines) + "\n"


def generate(out_dir: Path = DATA_DIR, data_dir: Path = DATA_DIR) -> None:
    rng = random.Random(SEED)
    master = json.loads((data_dir / "master_data.json").read_text(encoding="utf-8"))
    contracts_dir = out_dir / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    for c in build_contracts(rng, master, ordered_materials(data_dir, master)):
        number = c["customer"]["customer_number"]
        (contracts_dir / f"{number}.md").write_text(
            render_contract(c), encoding="utf-8"
        )
        if c["amendment"]:
            (contracts_dir / f"{number}-amendment-1.md").write_text(
                render_amendment(c), encoding="utf-8"
            )


if __name__ == "__main__":
    generate()
    print(f"Wrote contracts to {DATA_DIR / 'contracts'}")
