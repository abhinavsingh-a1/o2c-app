"""Generate non-PO documents for the document classifier.

Writes into poc/data/:
  other/DOC-XXXX.pdf   30 documents: 10 supplier invoices, 10 remittance advices,
                       10 others (order confirmations, delivery notes, newsletters)
  doc_types.json       file name -> purchase_order | invoice | remittance | other,
                       for these 30 files and the 50 POs in pos/

Some documents are deliberately confusing: order confirmations and delivery notes
repeat a real customer PO number and its lines, invoices to us carry "Your order"
references and item tables, remittances quote the customer's PO numbers.

Needs master_data.json and ground_truth/ from generate_pos.py.
Run with: uv run --group poc python poc/generate_other_docs.py
No network or LLM calls; the output is identical on every run.
"""

import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from generate_pos import (
    CELL,
    CENT,
    DATA_DIR,
    HEADING,
    NUM_POS,
    SMALL,
    SUPPLIER_ADDRESS,
    SUPPLIER_NAME,
    TITLE,
    build_pdf,
    fmt_date,
    fmt_number,
    fmt_quantity,
    multiline,
    p,
    write_json,
)
from reportlab.lib import colors
from reportlab.platypus import Spacer, Table, TableStyle
from schema import PurchaseOrder

SEED = 4711
VAT = Decimal("0.20")

# Companies that sell to Alpentech and send it invoices.
VENDORS = [
    ("Ostalpen Logistik GmbH", "Terminalstraße 3, 4600 Wels, Austria"),
    ("Kartonagen Huber GmbH", "Papierweg 8, 4481 Asten, Austria"),
    ("Innviertler Schraubenwerk GmbH", "Werkstraße 22, 4910 Ried im Innkreis, Austria"),
    ("TechnoLube Schmierstoffe GmbH", "Ölhafen 6, 68159 Mannheim, Germany"),
    ("Elektro Mayrhofer KG", "Bahnhofstraße 41, 4614 Marchtrenk, Austria"),
]

# Services and goods a vendor invoices: (description, unit, price range).
VENDOR_ITEMS = {
    "Ostalpen Logistik GmbH": [
        ("Stückgutversand Wels - Graz, 3 Paletten", "PC", (180, 260)),
        ("Expresszustellung Linz", "PC", (45, 90)),
        ("Palettentausch EUR-Paletten", "PC", (8, 12)),
        ("Treibstoffzuschlag", "PC", (15, 40)),
    ],
    "Kartonagen Huber GmbH": [
        ("Faltkarton 600x400x400 mm, 2-wellig", "PC", (1, 3)),
        ("Faltkarton 400x300x200 mm, 1-wellig", "PC", (1, 2)),
        ("Packpapier 75 cm Rolle 10 kg", "ROL", (18, 26)),
    ],
    "Innviertler Schraubenwerk GmbH": [
        ("Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk", "PAK", (8, 11)),
        ("Sechskantmutter DIN 934 M8 verzinkt, 100 Stk", "PAK", (3, 5)),
        ("Unterlegscheibe DIN 125 A 8,4 verzinkt, 100 Stk", "PAK", (1, 3)),
    ],
    "TechnoLube Schmierstoffe GmbH": [
        ("Hydrauliköl HLP 46, 208 l Fass", "PC", (480, 560)),
        ("Hochleistungsfett Lithium EP2, 400 g Kartusche", "PC", (3, 5)),
    ],
    "Elektro Mayrhofer KG": [
        ("Wartung Hallenbeleuchtung Lager 2", "H", (65, 85)),
        ("Austausch Sicherungsautomat B16", "PC", (25, 40)),
        ("Anfahrtspauschale", "PC", (35, 50)),
    ],
}

TEXT = {
    "de": {
        "invoice": "Rechnung",
        "invoice_no": "Rechnungsnummer",
        "date": "Datum",
        "your_order": "Ihre Bestellung",
        "customer_no": "Kundennummer",
        "items": ["Pos.", "Bezeichnung", "Menge", "ME", "Einzelpreis", "Betrag"],
        "net": "Nettobetrag",
        "vat": "zzgl. 20 % USt.",
        "gross": "Rechnungsbetrag",
        "terms": "Zahlbar innerhalb von 30 Tagen ohne Abzug.",
        "remittance": "Zahlungsavis",
        "payment_date": "Zahlungsdatum",
        "remit_intro": "wir haben heute folgende Rechnungen zur Zahlung angewiesen:",
        "remit_cols": [
            "Ihre Rechnung",
            "Rechnungsdatum",
            "Unsere Bestellung",
            "Brutto",
            "Skonto",
            "Zahlbetrag",
        ],
        "remit_total": "Überweisungsbetrag",
        "confirmation": "Auftragsbestätigung",
        "confirm_intro": "vielen Dank für Ihre Bestellung {po} vom {date}. Wir bestätigen wie folgt:",
        "delivery_note": "Lieferschein",
        "delivery_no": "Lieferschein-Nr.",
        "delivery_cols": ["Pos.", "Artikelnr.", "Bezeichnung", "Menge", "ME"],
        "packages": "Packstücke: {n}, Gesamtgewicht ca. {kg} kg",
        "greeting": "Sehr geehrte Damen und Herren,",
        "regards": "Mit freundlichen Grüßen",
    },
    "en": {
        "invoice": "Invoice",
        "invoice_no": "Invoice No.",
        "date": "Date",
        "your_order": "Your order",
        "customer_no": "Customer No.",
        "items": ["Item", "Description", "Qty", "Unit", "Unit Price", "Amount"],
        "net": "Net amount",
        "vat": "VAT 20 %",
        "gross": "Invoice total",
        "terms": "Payment due within 30 days net.",
        "remittance": "Remittance Advice",
        "payment_date": "Payment date",
        "remit_intro": "today we have transferred payment for the following invoices:",
        "remit_cols": [
            "Your invoice",
            "Invoice date",
            "Our PO",
            "Gross",
            "Discount",
            "Paid",
        ],
        "remit_total": "Total payment",
        "confirmation": "Order Confirmation",
        "confirm_intro": "thank you for your purchase order {po} dated {date}. We confirm as follows:",
        "delivery_note": "Delivery Note",
        "delivery_no": "Delivery Note No.",
        "delivery_cols": ["Item", "Material", "Description", "Qty", "Unit"],
        "packages": "Packages: {n}, total weight approx. {kg} kg",
        "greeting": "Dear Sir or Madam,",
        "regards": "Kind regards",
    },
}

# German documents use German formats, English ones English formats.
FORMATS = {"de": ("dmy", "de"), "en": ("text", "en")}

TABLE_STYLE = [
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
]


def table(rows: list, widths: list[float], numbers_from: int) -> Table:
    """Item table; columns from `numbers_from` on are right-aligned numbers."""
    style = [*TABLE_STYLE, ("ALIGN", (numbers_from, 0), (-1, -1), "RIGHT")]
    return Table(rows, colWidths=widths, style=style)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def random_date(rng: random.Random) -> date:
    return date(2026, 1, 5) + timedelta(days=rng.randint(0, 280))


def letterhead(
    sender: str, address: str, recipient: str, recipient_address: str
) -> list:
    return [
        p(sender, HEADING),
        p(address, SMALL),
        Spacer(1, 18),
        p(f"{recipient}\n{multiline(recipient_address)}"),
        Spacer(1, 18),
    ]


def meta_table(rows: list[tuple[str, str]]) -> Table:
    return Table([[f"{k}:", v] for k, v in rows], colWidths=[130, 200], hAlign="LEFT")


def totals_table(rows: list[tuple[str, str]]) -> Table:
    table = Table(rows, colWidths=[400, 95])
    table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    return table


def load_orders(data_dir: Path) -> list[PurchaseOrder]:
    return [
        PurchaseOrder.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((data_dir / "ground_truth").glob("PO-*.json"))
    ]


# --- document types ---------------------------------------------------------


def render_invoice(rng: random.Random, lang: str, path: Path, **_) -> None:
    """A vendor invoices Alpentech; it quotes Alpentech's own PO number."""
    t, (df, nf) = TEXT[lang], FORMATS[lang]
    vendor, vendor_address = rng.choice(VENDORS)
    items = rng.sample(VENDOR_ITEMS[vendor], rng.randint(1, len(VENDOR_ITEMS[vendor])))
    invoice_date = random_date(rng)

    rows, net = [t["items"]], Decimal(0)
    for n, (description, unit, (low, high)) in enumerate(items, start=1):
        quantity = Decimal(rng.randint(1, 40))
        price = money(Decimal(str(rng.uniform(low, high))))
        amount = money(quantity * price)
        net += amount
        rows.append(
            [
                str(n),
                p(description, CELL),
                fmt_quantity(quantity, nf),
                unit,
                fmt_number(price, nf),
                fmt_number(amount, nf),
            ]
        )
    vat = money(net * VAT)

    story = letterhead(vendor, vendor_address, SUPPLIER_NAME, SUPPLIER_ADDRESS)
    story += [
        p(t["invoice"], TITLE),
        meta_table(
            [
                (t["invoice_no"], f"RE-2026-{rng.randint(1000, 9999)}"),
                (t["date"], fmt_date(invoice_date, df)),
                (t["customer_no"], f"{rng.randint(20000, 29999)}"),
                (t["your_order"], f"EK-{rng.randint(10000, 99999)}"),
            ]
        ),
        Spacer(1, 14),
        table(rows, [30, 225, 45, 35, 70, 90], numbers_from=2),
        totals_table(
            [
                [t["net"], f"EUR {fmt_number(net, nf)}"],
                [t["vat"], f"EUR {fmt_number(vat, nf)}"],
                [t["gross"], f"EUR {fmt_number(net + vat, nf)}"],
            ]
        ),
        Spacer(1, 16),
        p(t["terms"], SMALL),
    ]
    build_pdf(path, story)


def render_remittance(
    rng: random.Random, lang: str, path: Path, orders: list[PurchaseOrder], **_
) -> None:
    """A customer tells Alpentech which invoices it paid, quoting its PO numbers."""
    t, (df, nf) = TEXT[lang], FORMATS[lang]
    order = rng.choice(orders)
    customer_orders = [o for o in orders if o.customer_name == order.customer_name]
    paid = rng.sample(customer_orders, min(len(customer_orders), rng.randint(1, 4)))
    payment_date = max(o.po_date for o in paid) + timedelta(days=rng.randint(30, 60))
    with_discount = rng.random() < 0.5

    rows, total = [t["remit_cols"]], Decimal(0)
    for o in paid:
        gross = money(o.total_amount * (1 + VAT))
        discount = money(gross * Decimal("0.02")) if with_discount else Decimal("0.00")
        total += gross - discount
        rows.append(
            [
                f"AR-2026-{rng.randint(10000, 99999)}",
                fmt_date(o.po_date + timedelta(days=rng.randint(3, 20)), df),
                o.po_number,
                fmt_number(gross, nf),
                fmt_number(discount, nf),
                fmt_number(gross - discount, nf),
            ]
        )

    story = letterhead(
        order.customer_name, order.ship_to_address, SUPPLIER_NAME, SUPPLIER_ADDRESS
    )
    story += [
        p(t["remittance"], TITLE),
        meta_table([(t["payment_date"], fmt_date(payment_date, df))]),
        Spacer(1, 10),
        p(t["greeting"]),
        p(t["remit_intro"]),
        Spacer(1, 8),
        table(rows, [85, 80, 95, 80, 70, 85], numbers_from=3),
        totals_table([[t["remit_total"], f"EUR {fmt_number(total, nf)}"]]),
        Spacer(1, 16),
        p(f"{t['regards']}\n{order.customer_name}"),
    ]
    build_pdf(path, story)


def render_confirmation(
    rng: random.Random, lang: str, path: Path, orders: list[PurchaseOrder], **_
) -> None:
    """Alpentech confirms a real PO: same PO number, same lines, same total."""
    t, (df, nf) = TEXT[lang], FORMATS[lang]
    order = rng.choice(orders)
    rows = [t["items"]]
    for line in order.lines:
        rows.append(
            [
                str(line.line_no),
                p(line.description, CELL),
                fmt_quantity(line.quantity, nf),
                line.unit,
                fmt_number(line.unit_price, nf),
                fmt_number(line.line_total, nf),
            ]
        )
    story = letterhead(
        SUPPLIER_NAME, SUPPLIER_ADDRESS, order.customer_name, order.ship_to_address
    )
    story += [
        p(t["confirmation"], TITLE),
        meta_table(
            [
                (
                    f"{t['confirmation']} Nr." if lang == "de" else "Confirmation No.",
                    f"AB-{rng.randint(100000, 999999)}",
                ),
                (t["date"], fmt_date(order.po_date + timedelta(days=1), df)),
                (t["your_order"], order.po_number),
            ]
        ),
        Spacer(1, 10),
        p(t["greeting"]),
        p(
            t["confirm_intro"].format(
                po=order.po_number, date=fmt_date(order.po_date, df)
            )
        ),
        Spacer(1, 8),
        table(rows, [30, 225, 45, 35, 70, 90], numbers_from=2),
        totals_table([[t["net"], f"EUR {fmt_number(order.total_amount, nf)}"]]),
        Spacer(1, 16),
        p(f"{t['regards']}\n{SUPPLIER_NAME}"),
    ]
    build_pdf(path, story)


def render_delivery_note(
    rng: random.Random, lang: str, path: Path, orders: list[PurchaseOrder], **_
) -> None:
    """Alpentech ships a real PO: PO number and quantities, but no prices."""
    t, (df, nf) = TEXT[lang], FORMATS[lang]
    order = rng.choice(orders)
    rows = [t["delivery_cols"]]
    for line in order.lines:
        rows.append(
            [
                str(line.line_no),
                line.material_number or "",
                p(line.description, CELL),
                fmt_quantity(line.quantity, nf),
                line.unit,
            ]
        )
    story = letterhead(
        SUPPLIER_NAME, SUPPLIER_ADDRESS, order.customer_name, order.ship_to_address
    )
    story += [
        p(t["delivery_note"], TITLE),
        meta_table(
            [
                (t["delivery_no"], f"LS-{rng.randint(100000, 999999)}"),
                (
                    t["date"],
                    fmt_date(order.po_date + timedelta(days=rng.randint(2, 10)), df),
                ),
                (t["your_order"], order.po_number),
            ]
        ),
        Spacer(1, 14),
        table(rows, [30, 60, 290, 60, 40], numbers_from=3),
        Spacer(1, 12),
        p(t["packages"].format(n=rng.randint(1, 6), kg=rng.randint(5, 400))),
    ]
    build_pdf(path, story)


def render_newsletter(
    rng: random.Random, lang: str, path: Path, materials: list[dict], **_
) -> None:
    """Alpentech promotion with article numbers and prices, inviting orders."""
    nf = FORMATS[lang][1]
    offers = rng.sample(materials, 4)
    percent = rng.choice([10, 15, 20])
    if lang == "de":
        title = f"Herbstaktion: {percent} % Rabatt auf ausgewählte Artikel"
        intro = "Bestellen Sie bis Ende des Monats und sichern Sie sich unsere Aktionspreise:"
        cols = ["Artikelnr.", "Bezeichnung", "Listenpreis", "Aktionspreis"]
        outro = "Bestellungen bitte unter Angabe Ihrer Kundennummer an bestellung@alpentech.example."
    else:
        title = f"Autumn Sale: {percent}% off selected items"
        intro = "Order by the end of the month to get our sale prices:"
        cols = ["Item No.", "Description", "List price", "Sale price"]
        outro = "Please send purchase orders quoting your customer number to orders@alpentech.example."
    rows = [cols]
    for m in offers:
        price = Decimal(m["list_price"])
        rows.append(
            [
                m["material_number"],
                p(m["description"], CELL),
                fmt_number(price, nf),
                fmt_number(money(price * (1 - Decimal(percent) / 100)), nf),
            ]
        )
    story = [
        p(f"{SUPPLIER_NAME} · Newsletter", HEADING),
        Spacer(1, 6),
        p(title, TITLE),
        p(intro),
        Spacer(1, 8),
        table(rows, [70, 265, 80, 80], numbers_from=2),
        Spacer(1, 12),
        p(outro),
        Spacer(1, 20),
        p("Abmelden / Unsubscribe: newsletter@alpentech.example", SMALL),
    ]
    build_pdf(path, story)


# kind -> (renderer, doc type, count)
KINDS = {
    "invoice": (render_invoice, "invoice", 10),
    "remittance": (render_remittance, "remittance", 10),
    "confirmation": (render_confirmation, "other", 4),
    "delivery_note": (render_delivery_note, "other", 3),
    "newsletter": (render_newsletter, "other", 3),
}


def generate(out_dir: Path = DATA_DIR, data_dir: Path = DATA_DIR) -> None:
    rng = random.Random(SEED)
    master = json.loads((data_dir / "master_data.json").read_text(encoding="utf-8"))
    orders = load_orders(data_dir)
    other_dir = out_dir / "other"
    other_dir.mkdir(parents=True, exist_ok=True)

    # Shuffle so a document's type can't be guessed from its file number.
    kinds = [kind for kind, (_, _, count) in KINDS.items() for _ in range(count)]
    rng.shuffle(kinds)

    doc_types = {f"PO-{i:04d}.pdf": "purchase_order" for i in range(1, NUM_POS + 1)}
    for i, kind in enumerate(kinds, start=1):
        render, doc_type, _ = KINDS[kind]
        name = f"DOC-{i:04d}.pdf"
        lang = rng.choice(["de", "en"])
        render(
            rng, lang, other_dir / name, orders=orders, materials=master["materials"]
        )
        doc_types[name] = doc_type
    write_json(out_dir / "doc_types.json", doc_types)


if __name__ == "__main__":
    generate()
    print(
        f"Wrote {sum(c for _, _, c in KINDS.values())} documents to {DATA_DIR / 'other'}"
    )
