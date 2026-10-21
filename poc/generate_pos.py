"""Generate a deterministic synthetic purchase-order dataset for the PoC.

Writes into poc/data/:
  master_data.json            customers and materials (loaded later by the ERP mock)
  pos/PO-XXXX.pdf             50 purchase orders in 6 customer layouts
  ground_truth/PO-XXXX.json   exact PurchaseOrder for each PDF
  labels.json                 which POs contain a price deviation, and on which line

Run with: uv run --group poc python poc/generate_pos.py
No network or LLM calls; the output is identical on every run.

Options create the ML datasets (ml/data) with the same customers, materials and rules:
  --seed N    random seed (default 42)
  --count N   number of purchase orders (default 50); count // 5 get a price deviation
  --out DIR   output directory (default poc/data). Other directories get manifest.json
              (seed and layout per PO) instead of a copy of master_data.json.
  --hard      only the two layouts never used in poc/data or ml/data/train, about half
              of the POs with 8-14 lines over two pages (table header repeated), terms
              and conditions, a bill-to address and a bank-details / "page x of y" footer.
Without options the script reproduces poc/data byte for byte.
"""

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from schema import LineItem, PurchaseOrder

# Without this, reportlab embeds the current timestamp and a random document ID.
rl_config.invariant = 1

SEED = 42
NUM_POS = 50
NUM_DEVIATIONS = 10
DATA_DIR = Path(__file__).parent / "data"
CENT = Decimal("0.01")

# We are the supplier receiving the orders.
SUPPLIER_NAME = "Alpentech Industriebedarf GmbH"
SUPPLIER_ADDRESS = "Lagerstraße 5, 4600 Wels, Austria"

CUSTOMERS = [
    {
        "customer_number": "1000101",
        "name": "Steirische Metallbau GmbH",
        "address": "Industriestraße 12, 8020 Graz, Austria",
        "currency": "EUR",
    },
    {
        "customer_number": "1000102",
        "name": "Donautal Maschinenbau AG",
        "address": "Hafenstraße 45, 4020 Linz, Austria",
        "currency": "EUR",
    },
    {
        "customer_number": "1000103",
        "name": "Alpenland Fördertechnik GmbH",
        "address": "Gewerbepark 7, 6020 Innsbruck, Austria",
        "currency": "EUR",
    },
    {
        "customer_number": "1000104",
        "name": "Wienerwald Anlagenbau GmbH",
        "address": "Brunner Straße 88, 1230 Wien, Austria",
        "currency": "EUR",
    },
    {
        "customer_number": "1000105",
        "name": "Bayerische Präzisionsteile GmbH",
        "address": "Lindenweg 3, 85053 Ingolstadt, Germany",
        "currency": "EUR",
    },
    {
        "customer_number": "1000106",
        "name": "Schwarzwald Antriebstechnik KG",
        "address": "Am Kanal 19, 79108 Freiburg im Breisgau, Germany",
        "currency": "EUR",
    },
    {
        "customer_number": "1000107",
        "name": "Salzach Kunststofftechnik GmbH",
        "address": "Münchner Bundesstraße 140, 5020 Salzburg, Austria",
        "currency": "EUR",
    },
    {
        "customer_number": "1000108",
        "name": "Niederrhein Stahlhandel GmbH",
        "address": "Hafenweg 21, 47119 Duisburg, Germany",
        "currency": "EUR",
    },
]

# (material_number, description, unit, list_price)
MATERIALS = [
    ("400101", "Sechskantschraube DIN 933 M8x30 verzinkt, 100 Stk", "PAK", "14.90"),
    ("400102", "Sechskantmutter DIN 934 M8 verzinkt, 100 Stk", "PAK", "6.40"),
    ("400103", "Unterlegscheibe DIN 125 A 8,4 verzinkt, 100 Stk", "PAK", "3.20"),
    ("400104", "Rillenkugellager 6205-2RS C3", "PC", "8.75"),
    ("400105", "Hydraulikschlauch DN10 2SN, Meterware", "M", "12.60"),
    ("400106", "Druckluftschlauch PU 8x6 mm blau", "M", "1.85"),
    ("400107", "Kabelbinder 4,8x200 mm schwarz, 100 Stk", "PAK", "4.90"),
    ("400108", "Schutzhandschuhe Nitril Gr. 9, 12 Paar", "BOX", "27.50"),
    ("400109", "Schweißdraht SG2 1,0 mm, 15 kg Spule", "PC", "46.80"),
    ("400110", "Trennscheibe 125x1,0 mm INOX", "PC", "1.35"),
    ("400111", "Schruppscheibe 125x6 mm Stahl", "PC", "3.90"),
    ("400112", "Hochleistungsfett Lithium EP2, 400 g Kartusche", "PC", "7.20"),
    ("400113", "Hydrauliköl HLP 46", "L", "3.45"),
    ("400114", "Industriereiniger Konzentrat, 10 l Kanister", "PC", "38.90"),
    ("400115", "Gewindebohrer-Satz M3-M12 HSS-E", "PC", "89.00"),
    ("400116", "Spiralbohrer HSS-G 8,0 mm DIN 338, 10 Stk", "PAK", "18.60"),
    ("400117", "Keilriemen SPZ 1250", "PC", "9.80"),
    ("400118", "Rollenkette 08B-1 Simplex, 5 m", "PC", "54.00"),
    ("400119", "Gewebeklebeband 50 mm x 50 m silber", "ROL", "6.25"),
    ("400120", "Stretchfolie 500 mm x 300 m, 23 my", "ROL", "16.40"),
    ("400121", "Edelstahlrohr 1.4301 25x2 mm", "M", "18.70"),
    ("400122", "Flachstahl S235 40x5 mm", "KG", "2.15"),
    ("400123", "Gehörschutzstöpsel, 200 Paar", "BOX", "32.00"),
    ("400124", "Mantelleitung NYM-J 3x1,5 mm², 100 m Ring", "PC", "89.90"),
    ("400125", "LED-Hallenstrahler 150 W IP65", "PC", "129.00"),
]

MONTHS_EN = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
MONTHS_DE = [
    "Jänner",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


@dataclass(frozen=True)
class Layout:
    name: str
    # "dmy" (15.10.2026), "iso" (2026-10-15), "text" (15 Oct 2026), "long_de" (15. Oktober 2026)
    date_fmt: str
    num_fmt: str  # "en" (1,234.56) or "de" (1.234,56)
    shows_customer_number: bool


LAYOUTS = {
    "table": Layout("table", "dmy", "en", True),
    "letter": Layout("letter", "text", "en", True),
    "two_column": Layout("two_column", "iso", "en", False),
    "compact": Layout("compact", "iso", "de", True),
    "german": Layout("german", "dmy", "de", True),
    "landscape": Layout("landscape", "text", "de", False),
}

# --hard only: layouts that appear in no training data.
HARD_LAYOUTS = {
    "portal": Layout("portal", "iso", "en", True),
    "formular": Layout("formular", "long_de", "de", False),
}
LONG_ORDER_LINES = 8  # --hard orders with at least this many lines span two pages

# Each customer always sends the same template, like real customers do.
CUSTOMER_PROFILES = {
    "1000101": ("german", "4500{:06d}"),
    "1000102": ("table", "PO-2026-{:04d}"),
    "1000103": ("two_column", "AFT/{:05d}"),
    "1000104": ("letter", "WA-26-{:04d}"),
    "1000105": ("compact", "BP{:07d}"),
    "1000106": ("landscape", "SAT-{:06d}"),
    "1000107": ("german", "B-26/{:04d}"),
    "1000108": ("table", "NSH{:06d}"),
}


@dataclass
class Order:
    po_id: str
    po: PurchaseOrder
    layout: Layout
    cost_center: str
    extras: dict = field(default_factory=dict)  # --hard only: noise for the new layouts


# --- formatting -------------------------------------------------------------


def fmt_date(d: date, style: str) -> str:
    if style == "dmy":
        return d.strftime("%d.%m.%Y")
    if style == "iso":
        return d.isoformat()
    if style == "long_de":
        return f"{d.day}. {MONTHS_DE[d.month - 1]} {d.year}"
    return f"{d.day:02d} {MONTHS_EN[d.month - 1]} {d.year}"


def fmt_number(value: Decimal, style: str, places: int = 2) -> str:
    text = f"{value:,.{places}f}"
    if style == "de":
        text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return text


def fmt_quantity(value: Decimal, style: str) -> str:
    places = max(0, -value.normalize().as_tuple().exponent)
    return fmt_number(value, style, places)


def city_of(address: str) -> str:
    """'Brunner Straße 88, 1230 Wien, Austria' -> 'Wien'."""
    return address.split(", ")[1].split(" ", 1)[1]


# --- data -------------------------------------------------------------------


def master_data() -> dict:
    return {
        "customers": CUSTOMERS,
        "materials": [
            {"material_number": n, "description": d, "unit": u, "list_price": p}
            for n, d, u, p in MATERIALS
        ],
    }


def random_quantity(rng: random.Random, unit: str) -> Decimal:
    if unit in ("M", "KG"):
        return Decimal(rng.randint(10, 400)) / 2  # allows values like 12.5
    if unit == "L":
        return Decimal(rng.choice([20, 40, 60, 100, 200, 208]))
    return Decimal(rng.randint(1, 60))


def deviating_price(rng: random.Random, list_price: Decimal) -> Decimal:
    """A price 5-30% above or below list price, still in range after rounding."""
    while True:
        factor = Decimal(str(round(rng.uniform(0.05, 0.30), 4)))
        sign = rng.choice([1, -1])
        price = (list_price * (1 + sign * factor)).quantize(CENT, ROUND_HALF_UP)
        deviation = abs(price - list_price) / list_price
        if Decimal("0.05") <= deviation <= Decimal("0.30"):
            return price


def noise_extras(rng: random.Random, customer: dict) -> dict:
    """Numbers that look relevant but are not part of the ground truth."""
    country = "DE" if customer["address"].endswith("Germany") else "AT"
    digits = 18 if country == "DE" else 16  # IBAN length 22 (DE) or 20 (AT)
    account = "".join(str(rng.randint(0, 9)) for _ in range(digits))
    iban = f"{country}{rng.randint(10, 99)}{account}"
    return {
        "iban": " ".join(iban[i : i + 4] for i in range(0, len(iban), 4)),
        "bic": rng.choice(["RZOOAT2L", "BKAUATWW", "GIBAATWW", "COBADEFF", "BYLADEM1"]),
        "vat_id": f"{'ATU' if country == 'AT' else 'DE'}{rng.randint(10000000, 99999999)}",
        "register": f"FN {rng.randint(100000, 599999)}{rng.choice('abdghkmpstvw')}",
        "requisition": f"REQ-{rng.randint(100000, 999999)}",
        "supplier_no": f"7{rng.randint(1000000, 9999999)}",
        "po_box": str(rng.randint(10, 999)),
        "extension": str(rng.randint(100, 899)),
        "dock": str(rng.randint(1, 12)),
    }


def build_orders(
    rng: random.Random,
    count: int = NUM_POS,
    num_deviations: int = NUM_DEVIATIONS,
    hard: bool = False,
) -> tuple[list[Order], dict]:
    deviating = set(rng.sample(range(1, count + 1), num_deviations))
    # --hard: exactly half of the orders are long enough to span two pages.
    long_orders = set(rng.sample(range(1, count + 1), count // 2)) if hard else set()
    orders: list[Order] = []
    labels: dict = {}

    for i in range(1, count + 1):
        po_id = f"PO-{i:04d}"
        customer = rng.choice(CUSTOMERS)
        layout_name, po_pattern = CUSTOMER_PROFILES[customer["customer_number"]]
        layout = LAYOUTS[layout_name]
        if hard:
            layout = HARD_LAYOUTS[rng.choice(sorted(HARD_LAYOUTS))]

        po_date = date(2026, 1, 5) + timedelta(days=rng.randint(0, 280))
        delivery_date = (
            po_date + timedelta(days=rng.randint(7, 42)) if rng.random() < 0.7 else None
        )

        if not hard:
            num_lines = rng.randint(1, 8)
        elif i in long_orders:
            num_lines = rng.randint(LONG_ORDER_LINES, 14)
        else:
            num_lines = rng.randint(1, LONG_ORDER_LINES - 1)
        materials = rng.sample(MATERIALS, num_lines)
        deviant_line = rng.randint(1, len(materials)) if i in deviating else None

        lines = []
        for line_no, (number, description, unit, list_price) in enumerate(
            materials, start=1
        ):
            quantity = random_quantity(rng, unit)
            price = Decimal(list_price)
            if line_no == deviant_line:
                price = deviating_price(rng, price)
            lines.append(
                LineItem(
                    line_no=line_no,
                    material_number=number if rng.random() < 0.6 else None,
                    description=description,
                    quantity=quantity,
                    unit=unit,
                    unit_price=price,
                    line_total=(quantity * price).quantize(CENT, ROUND_HALF_UP),
                )
            )

        po = PurchaseOrder(
            po_number=po_pattern.format(rng.randint(1, 99999)),
            po_date=po_date,
            customer_name=customer["name"],
            customer_number=customer["customer_number"]
            if layout.shows_customer_number
            else None,
            currency=customer["currency"],
            delivery_date=delivery_date,
            ship_to_address=customer["address"],
            lines=lines,
            total_amount=sum((line.line_total for line in lines), Decimal("0.00")),
        )
        order = Order(po_id, po, layout, f"KST-{rng.randint(1000, 9999)}")
        if hard:
            order.extras = noise_extras(rng, customer)
        orders.append(order)
        labels[po_id] = {
            "price_deviation": deviant_line is not None,
            "line_no": deviant_line,
        }

    return orders, labels


# --- PDF layouts ------------------------------------------------------------

STYLES = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=STYLES["Normal"], fontSize=9, leading=12)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7, leading=9)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10)
TITLE = ParagraphStyle("title", parent=STYLES["Title"], fontSize=18, alignment=0)
HEADING = ParagraphStyle("heading", parent=STYLES["Heading2"], fontSize=13)


def p(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def multiline(address: str) -> str:
    return address.replace(", ", "\n")


def footer_canvas(lines: list[str], page_label: str) -> type[canvas.Canvas]:
    """Canvas that prints footer lines and e.g. "Page 1 of 2" on every page.

    The total page count is only known at the end, so pages are buffered until save().
    """

    class FooterCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages: list[dict] = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self.setFont("Helvetica", 6.5)
                for n, text in enumerate(reversed(lines)):
                    self.drawString(40, 20 + 8 * n, text)
                self.drawRightString(
                    self._pagesize[0] - 40,
                    20 + 8 * len(lines),
                    page_label.format(page=self._pageNumber, total=total),
                )
                super().showPage()
            super().save()

    return FooterCanvas


def build_pdf(
    path: Path,
    story: list,
    pagesize=A4,
    margin: float = 50,
    canvasmaker: type[canvas.Canvas] | None = None,
) -> None:
    doc = SimpleDocTemplate(
        str(path),
        pagesize=pagesize,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin + (20 if canvasmaker else 0),  # room for the footer
        title=path.stem,
        author="",
        creator="",
    )
    if canvasmaker:
        doc.build(story, canvasmaker=canvasmaker)
    else:
        doc.build(story)


def items_table(
    order: Order,
    headers: list[str],
    widths: list[float],
    style: list,
    cell: ParagraphStyle = CELL,
    extra=None,
) -> Table:
    """Standard line-item table; `extra` adds a trailing per-line column."""
    nf = order.layout.num_fmt
    rows = [headers]
    for line in order.po.lines:
        row = [
            str(line.line_no),
            line.material_number or "",
            p(line.description, cell),
            fmt_quantity(line.quantity, nf),
            line.unit,
            fmt_number(line.unit_price, nf),
            fmt_number(line.line_total, nf),
        ]
        if extra:
            row.append(extra(line))
        rows.append(row)
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), cell.fontSize),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                ("ALIGN", (5, 1), (6, -1), "RIGHT"),
                *style,
            ]
        )
    )
    return table


def layout_table(order: Order, path: Path) -> None:
    """English, classic boxed header and a striped item table."""
    po, lay = order.po, order.layout
    meta = [
        ["PO Number:", po.po_number],
        ["PO Date:", fmt_date(po.po_date, lay.date_fmt)],
        ["Customer No.:", po.customer_number],
    ]
    if po.delivery_date:
        meta.append(["Delivery Date:", fmt_date(po.delivery_date, lay.date_fmt)])
    header = Table(
        [
            [p(f"{po.customer_name}\n{multiline(po.ship_to_address)}"), Table(meta)],
        ],
        colWidths=[275, 220],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (1, 0), (1, 0), 0.8, colors.black),
            ]
        )
    )
    items = items_table(
        order,
        ["Pos", "Item No.", "Description", "Qty", "Unit", "Unit Price", "Amount"],
        [30, 55, 180, 50, 35, 70, 75],
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b5c")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            (
                "ROWBACKGROUNDS",
                (0, 1),
                (-1, -1),
                [colors.white, colors.HexColor("#eef2f6")],
            ),
            ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black),
        ],
    )
    total = Table(
        [["Total (EUR, net):", fmt_number(po.total_amount, lay.num_fmt)]],
        colWidths=[420, 75],
        style=[
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ],
    )
    story = [
        p("PURCHASE ORDER", TITLE),
        header,
        Spacer(1, 12),
        p(f"Supplier:\n{SUPPLIER_NAME}\n{multiline(SUPPLIER_ADDRESS)}"),
        Spacer(1, 8),
        p(f"Ship to:\n{po.customer_name}\n{multiline(po.ship_to_address)}"),
        Spacer(1, 14),
        items,
        total,
        Spacer(1, 20),
        p("Please confirm this order within 3 working days. Currency: EUR.", SMALL),
    ]
    build_pdf(path, story)


def layout_letter(order: Order, path: Path) -> None:
    """Business letter; items are written as numbered sentences, not a table."""
    po, lay = order.po, order.layout
    nf = lay.num_fmt
    sender_line = f"{po.customer_name} · {po.ship_to_address}"
    story = [
        p(sender_line, SMALL),
        Spacer(1, 10),
        p(f"{SUPPLIER_NAME}\nSales Department\n{multiline(SUPPLIER_ADDRESS)}"),
        Spacer(1, 24),
        Paragraph(
            f"{escape(city_of(po.ship_to_address))}, {fmt_date(po.po_date, lay.date_fmt)}",
            ParagraphStyle("right", parent=BODY, alignment=2),
        ),
        Spacer(1, 12),
        p(f"Purchase Order {po.po_number}", HEADING),
        p(f"Our customer number with you: {po.customer_number}"),
        Spacer(1, 10),
        p("Dear Sir or Madam,"),
        Spacer(1, 6),
        p("we hereby order the following items under our general terms of purchase:"),
        Spacer(1, 8),
    ]
    for line in po.lines:
        item = (
            f" (your item no. {line.material_number})" if line.material_number else ""
        )
        story.append(
            p(
                f"{line.line_no}. {fmt_quantity(line.quantity, nf)} {line.unit} {line.description}{item} "
                f"at EUR {fmt_number(line.unit_price, nf)} per {line.unit}, "
                f"line value EUR {fmt_number(line.line_total, nf)}"
            )
        )
        story.append(Spacer(1, 4))
    story += [
        Spacer(1, 6),
        p(f"Total order value: EUR {fmt_number(po.total_amount, nf)} (net)."),
    ]
    if po.delivery_date:
        story.append(
            p(f"Requested delivery date: {fmt_date(po.delivery_date, lay.date_fmt)}.")
        )
    story += [
        p(f"Please deliver to: {po.ship_to_address}."),
        Spacer(1, 16),
        p("Kind regards,"),
        Spacer(1, 18),
        p(f"Purchasing Department\n{po.customer_name}"),
    ]
    build_pdf(path, story, margin=65)


def layout_two_column(order: Order, path: Path) -> None:
    """Coloured banner, buyer/supplier side by side, then order details/ship-to."""
    po, lay = order.po, order.layout
    banner = Table(
        [
            [
                p(
                    po.customer_name,
                    ParagraphStyle("b", parent=HEADING, textColor=colors.white),
                ),
                p(
                    "ORDER",
                    ParagraphStyle(
                        "o", parent=HEADING, textColor=colors.white, alignment=2
                    ),
                ),
            ]
        ],
        colWidths=[300, 195],
        style=[("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#2e6b4f"))],
    )
    details = (
        f"Order No.: {po.po_number}\nOrder Date: {fmt_date(po.po_date, lay.date_fmt)}"
    )
    if po.delivery_date:
        details += f"\nRequired By: {fmt_date(po.delivery_date, lay.date_fmt)}"
    grid = Table(
        [
            [p("BUYER", SMALL), p("SUPPLIER", SMALL)],
            [
                p(f"{po.customer_name}\n{multiline(po.ship_to_address)}"),
                p(f"{SUPPLIER_NAME}\n{multiline(SUPPLIER_ADDRESS)}"),
            ],
            [p("ORDER DETAILS", SMALL), p("SHIP TO", SMALL)],
            [p(details), p(f"Goods receiving\n{multiline(po.ship_to_address)}")],
        ],
        colWidths=[247, 248],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
            ("LINEBELOW", (0, 2), (-1, 2), 0.5, colors.grey),
            ("TOPPADDING", (0, 2), (-1, 2), 12),
        ],
    )
    # Material number in the 3rd column here; reorder the standard columns.
    nf = lay.num_fmt
    rows = [["Line", "Description", "Material", "Quantity", "UoM", "Price", "Total"]]
    for line in po.lines:
        rows.append(
            [
                str(line.line_no),
                p(line.description, CELL),
                line.material_number or "-",
                fmt_quantity(line.quantity, nf),
                line.unit,
                fmt_number(line.unit_price, nf),
                fmt_number(line.line_total, nf),
            ]
        )
    rows.append(["", "", "", "", "", "TOTAL EUR", fmt_number(po.total_amount, nf)])
    items = Table(
        rows,
        colWidths=[30, 190, 55, 55, 35, 60, 70],
        style=[
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#2e6b4f")),
            ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#2e6b4f")),
            ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ],
    )
    build_pdf(path, [banner, Spacer(1, 14), grid, Spacer(1, 18), items])


def layout_compact(order: Order, path: Path) -> None:
    """Dense, invoice-like grid with tiny fonts and narrow margins."""
    po, lay = order.po, order.layout
    head = Table(
        [
            [
                "Auftrag / Order",
                "Datum / Date",
                "Kd.-Nr. / Cust. No.",
                "Liefertermin / Delivery",
            ],
            [
                po.po_number,
                fmt_date(po.po_date, lay.date_fmt),
                po.customer_number,
                fmt_date(po.delivery_date, lay.date_fmt) if po.delivery_date else "n/a",
            ],
        ],
        colWidths=[136] * 4,
        style=[
            ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
            ("FONTSIZE", (0, 0), (-1, 0), 6),
            ("FONTSIZE", (0, 1), (-1, 1), 8),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ],
    )
    small_cell = ParagraphStyle("sc", parent=CELL, fontSize=7, leading=8.5)
    items = items_table(
        order,
        ["#", "Art.-Nr.", "Text", "Menge", "ME", "EP", "GP"],
        [20, 50, 260, 50, 30, 64, 70],
        [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ],
        cell=small_cell,
    )
    total = Table(
        [["Summe netto EUR", fmt_number(po.total_amount, lay.num_fmt)]],
        colWidths=[474, 70],
        style=[
            ("GRID", (1, 0), (1, 0), 0.4, colors.black),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ],
    )
    story = [
        Table(
            [
                [
                    p(f"{po.customer_name} | {po.ship_to_address}", SMALL),
                    p(f"An: {SUPPLIER_NAME}, {SUPPLIER_ADDRESS}", SMALL),
                ]
            ],
            colWidths=[272, 272],
        ),
        Spacer(1, 6),
        head,
        Spacer(1, 6),
        p(f"Lieferadresse / Ship to: {po.ship_to_address}", SMALL),
        Spacer(1, 6),
        items,
        total,
    ]
    build_pdf(path, story, margin=25)


def layout_german(order: Order, path: Path) -> None:
    """German-language 'Bestellung' with German labels and number formats."""
    po, lay = order.po, order.layout
    meta = [
        ["Bestellnummer:", po.po_number],
        ["Bestelldatum:", fmt_date(po.po_date, lay.date_fmt)],
        ["Kunden-Nr. bei Ihnen:", po.customer_number],
    ]
    if po.delivery_date:
        meta.append(["Liefertermin:", fmt_date(po.delivery_date, lay.date_fmt)])
    header = Table(
        [
            [
                p(f"{SUPPLIER_NAME}\n{multiline(SUPPLIER_ADDRESS)}"),
                Table(meta, style=[("FONTSIZE", (0, 0), (-1, -1), 9)]),
            ]
        ],
        colWidths=[265, 230],
        style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
    )
    items = items_table(
        order,
        [
            "Pos.",
            "Artikelnr.",
            "Bezeichnung",
            "Menge",
            "ME",
            "Einzelpreis",
            "Gesamtpreis",
        ],
        [32, 55, 178, 50, 30, 70, 80],
        [
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ],
    )
    total = Table(
        [["Gesamtsumme netto (EUR):", fmt_number(po.total_amount, lay.num_fmt)]],
        colWidths=[415, 80],
        style=[
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ],
    )
    story = [
        p(f"{po.customer_name} · {po.ship_to_address}", SMALL),
        Spacer(1, 8),
        p("Bestellung", TITLE),
        header,
        Spacer(1, 12),
        p(
            f"Lieferanschrift:\n{po.customer_name}\nWareneingang\n{multiline(po.ship_to_address)}"
        ),
        Spacer(1, 12),
        p(
            "Sehr geehrte Damen und Herren, wir bestellen zu unseren Allgemeinen Einkaufsbedingungen:"
        ),
        Spacer(1, 8),
        items,
        total,
        Spacer(1, 20),
        p(
            "Bitte geben Sie unsere Bestellnummer auf allen Lieferpapieren und Rechnungen an.",
            SMALL,
        ),
        p("Mit freundlichen Grüßen\nEinkauf", BODY),
    ]
    build_pdf(path, story)


def layout_landscape(order: Order, path: Path) -> None:
    """Landscape A4, three header columns and a wide table with a cost-centre column."""
    po, lay = order.po, order.layout
    order_info = (
        f"Purchase Order: {po.po_number}\nDate: {fmt_date(po.po_date, lay.date_fmt)}"
    )
    if po.delivery_date:
        order_info += f"\nDelivery: {fmt_date(po.delivery_date, lay.date_fmt)}"
    header = Table(
        [
            [p("FROM", SMALL), p("ORDER", SMALL), p("DELIVER TO", SMALL)],
            [
                p(f"{po.customer_name}\n{multiline(po.ship_to_address)}"),
                p(order_info),
                p(f"{po.customer_name}\n{multiline(po.ship_to_address)}"),
            ],
        ],
        colWidths=[247, 247, 248],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ],
    )
    items = items_table(
        order,
        [
            "Item",
            "Material No.",
            "Description",
            "Qty",
            "Unit",
            "Unit Price (EUR)",
            "Net Value (EUR)",
            "Cost Ctr.",
        ],
        [40, 75, 287, 60, 40, 85, 90, 65],
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6b2e2e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.lightgrey),
        ],
        extra=lambda line: order.cost_center,
    )
    total = Table(
        [["Total net value (EUR)", fmt_number(po.total_amount, lay.num_fmt), ""]],
        colWidths=[587, 90, 65],
        style=[
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ],
    )
    story = [
        p(f"PURCHASE ORDER — {po.customer_name}", HEADING),
        p(f"To: {SUPPLIER_NAME}, {SUPPLIER_ADDRESS}", SMALL),
        Spacer(1, 8),
        header,
        Spacer(1, 12),
        items,
        total,
    ]
    build_pdf(path, story, pagesize=landscape(A4), margin=50)


# --- --hard layouts ---------------------------------------------------------

HEAD_CELL = ParagraphStyle(
    "head_cell", parent=CELL, textColor=colors.white, fontName="Helvetica-Bold"
)

TERMS_EN = [
    (
        "1. This order is subject exclusively to the buyer's general terms of purchase; "
        "the supplier's terms of sale do not apply, even if they are not expressly rejected."
    ),
    (
        "2. The order number and the line number must be quoted on every delivery note and "
        "invoice. Invoices without a valid order number are returned unpaid."
    ),
    (
        "3. Payment within 14 days with 2 % discount or within 60 days net. Orders below "
        "EUR 100.00 net may be charged a handling fee of EUR 15.00."
    ),
    (
        "4. Deliveries are accepted Monday to Friday from 7:00 to 14:00. Partial deliveries "
        "require prior written approval."
    ),
    "5. Place of jurisdiction is the registered office of the buyer.",
]

TERMS_DE = [
    (
        "1. Es gelten ausschließlich unsere Allgemeinen Einkaufsbedingungen; abweichende "
        "Bedingungen des Lieferanten gelten nur bei schriftlicher Anerkennung."
    ),
    (
        "2. Bestellnummer und Positionsnummer sind auf allen Lieferscheinen und Rechnungen "
        "anzugeben. Rechnungen ohne Bestellnummer werden unbearbeitet retourniert."
    ),
    (
        "3. Zahlung innerhalb von 14 Tagen mit 3 % Skonto oder innerhalb von 60 Tagen netto. "
        "Mindermengenzuschläge für Aufträge unter EUR 150,00 werden nicht akzeptiert."
    ),
    "4. Warenannahme Montag bis Donnerstag 7:00 bis 15:00 Uhr, Freitag 7:00 bis 11:30 Uhr.",
    (
        "5. Gerichtsstand ist der Sitz des Bestellers. Es gilt österreichisches Recht unter "
        "Ausschluss des UN-Kaufrechts."
    ),
]


def address_parts(address: str) -> tuple[str, str, str]:
    """'Brunner Straße 88, 1230 Wien, Austria' -> ('Brunner Straße 88', '1230 Wien', 'Austria')."""
    street, city, country = address.split(", ")
    return street, city, country


def split_lines(po: PurchaseOrder) -> list[list[LineItem]]:
    """Long --hard orders are printed on two pages, the table header on both."""
    if len(po.lines) < LONG_ORDER_LINES:
        return [po.lines]
    half = (len(po.lines) + 1) // 2
    return [po.lines[:half], po.lines[half:]]


def head_row(headers: list[str]) -> list[Paragraph]:
    return [p(h, HEAD_CELL) for h in headers]


def layout_portal(order: Order, path: Path) -> None:
    """--hard: e-procurement export with bill-to address, need-by column and a legal footer."""
    po, lay, x = order.po, order.layout, order.extras
    nf = lay.num_fmt
    _, city, country = address_parts(po.ship_to_address)
    delivery = fmt_date(po.delivery_date, lay.date_fmt) if po.delivery_date else "-"
    head = Table(
        [
            [
                p("PURCHASE ORDER", TITLE),
                p(
                    f"Generated by the buyer's e-procurement portal\n"
                    f"Revision 0 · {x['requisition']}",
                    ParagraphStyle("portal_right", parent=SMALL, alignment=2),
                ),
            ]
        ],
        colWidths=[300, 215],
        style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
    )
    grey = colors.HexColor("#f0f0f0")
    meta = Table(
        [
            [
                p("Order number", SMALL),
                p(po.po_number),
                p("Order date", SMALL),
                p(fmt_date(po.po_date, lay.date_fmt)),
            ],
            [
                p("Buyer", SMALL),
                p(po.customer_name),
                p("Requisition", SMALL),
                p(x["requisition"]),
            ],
            [
                p("Account no. at supplier", SMALL),
                p(po.customer_number or ""),
                p("Need-by date", SMALL),
                p(delivery),
            ],
            [
                p("Supplier", SMALL),
                p(SUPPLIER_NAME),
                p("Currency", SMALL),
                p(po.currency),
            ],
        ],
        colWidths=[95, 165, 85, 170],
        style=[
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), grey),
            ("BACKGROUND", (2, 0), (2, -1), grey),
        ],
    )
    addresses = Table(
        [
            [p("BILL TO", SMALL), p("SHIP TO", SMALL)],
            [
                p(
                    f"{po.customer_name}\nAccounts Payable\nP.O. Box {x['po_box']}\n{city}\n{country}"
                ),
                p(
                    f"{po.customer_name}\nAttn: Goods receiving, dock {x['dock']}\n"
                    f"{multiline(po.ship_to_address)}"
                ),
            ],
        ],
        colWidths=[257, 258],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
        ],
    )
    headers = head_row(
        [
            "Ln",
            "Supplier part",
            "Description",
            "Need by",
            "Qty",
            "UOM",
            "Unit price",
            "Ext. price",
        ]
    )

    def items(lines: list[LineItem]) -> Table:
        rows = [headers]
        for line in lines:
            rows.append(
                [
                    str(line.line_no),
                    line.material_number or "",
                    p(line.description, CELL),
                    delivery if po.delivery_date else "",
                    fmt_quantity(line.quantity, nf),
                    line.unit,
                    fmt_number(line.unit_price, nf),
                    fmt_number(line.line_total, nf),
                ]
            )
        return Table(
            rows,
            colWidths=[24, 58, 170, 58, 42, 34, 59, 70],
            repeatRows=1,
            style=[
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3d3d6b")),
                ("ALIGN", (4, 1), (4, -1), "RIGHT"),
                ("ALIGN", (6, 1), (7, -1), "RIGHT"),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.lightgrey),
            ],
        )

    pages = split_lines(po)
    story = [
        head,
        Spacer(1, 8),
        meta,
        Spacer(1, 10),
        addresses,
        Spacer(1, 12),
        items(pages[0]),
    ]
    for part in pages[1:]:
        story += [
            PageBreak(),
            p(f"Purchase order {po.po_number} (continued)", SMALL),
            Spacer(1, 6),
            items(part),
        ]
    story += [
        Table(
            [["Order total (net, EUR)", fmt_number(po.total_amount, nf)]],
            colWidths=[445, 70],
            style=[
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
            ],
        ),
        p(
            "Freight and packaging included unless stated otherwise. VAT is invoiced separately.",
            SMALL,
        ),
        Spacer(1, 14),
        p("Terms and conditions", HEADING),
        *[p(term, SMALL) for term in TERMS_EN],
    ]
    footer = [
        f"{po.customer_name} · {po.ship_to_address}",
        f"Bank: IBAN {x['iban']} · BIC {x['bic']} · VAT ID {x['vat_id']}",
    ]
    build_pdf(
        path,
        story,
        margin=40,
        canvasmaker=footer_canvas(footer, "Page {page} of {total}"),
    )


def layout_formular(order: Order, path: Path) -> None:
    """--hard: German order form; quantity before description, carry-forward subtotals."""
    po, lay, x = order.po, order.layout, order.extras
    nf = lay.num_fmt
    _, city, country = address_parts(po.ship_to_address)
    delivery = (
        fmt_date(po.delivery_date, lay.date_fmt)
        if po.delivery_date
        else "nach Vereinbarung"
    )
    fields = Table(
        [
            [
                p("Bestell-Nr.", SMALL),
                p(po.po_number),
                p("Bestelldatum", SMALL),
                p(fmt_date(po.po_date, lay.date_fmt)),
            ],
            [
                p("Ihre Lieferantennummer bei uns", SMALL),
                p(x["supplier_no"]),
                p("Liefertermin", SMALL),
                p(delivery),
            ],
            [
                p("Sachbearbeitung", SMALL),
                p(f"Einkauf, Durchwahl -{x['extension']}"),
                p("Kostenstelle", SMALL),
                p(order.cost_center),
            ],
        ],
        colWidths=[110, 145, 90, 160],
        style=[
            ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ],
    )
    addresses = Table(
        [
            [
                p("Lieferant", SMALL),
                p("Rechnungsanschrift", SMALL),
                p("Lieferanschrift", SMALL),
            ],
            [
                p(f"{SUPPLIER_NAME}\n{multiline(SUPPLIER_ADDRESS)}"),
                p(
                    f"{po.customer_name}\nBuchhaltung\nPostfach {x['po_box']}\n{city}\n{country}"
                ),
                p(
                    f"{po.customer_name}\nWareneingang Tor {x['dock']}\n"
                    f"{multiline(po.ship_to_address)}"
                ),
            ],
        ],
        colWidths=[168, 168, 169],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
        ],
    )
    headers = head_row(
        [
            "Pos.",
            "Menge",
            "ME",
            "Artikel / Bezeichnung",
            "Ihre Art.-Nr.",
            "Preis/ME",
            "Gesamt",
        ]
    )

    def items(
        lines: list[LineItem], carried: Decimal | None, carry_out: Decimal | None
    ) -> Table:
        rows = [headers]
        if carried is not None:
            rows.append(
                ["", "", "", p("Übertrag", CELL), "", "", fmt_number(carried, nf)]
            )
        for line in lines:
            rows.append(
                [
                    str(line.line_no),
                    fmt_quantity(line.quantity, nf),
                    line.unit,
                    p(line.description, CELL),
                    line.material_number or "",
                    fmt_number(line.unit_price, nf),
                    fmt_number(line.line_total, nf),
                ]
            )
        if carry_out is not None:
            rows.append(
                [
                    "",
                    "",
                    "",
                    p("Zwischensumme, Übertrag auf Seite 2", CELL),
                    "",
                    "",
                    fmt_number(carry_out, nf),
                ]
            )
        return Table(
            rows,
            colWidths=[30, 48, 32, 199, 62, 58, 76],
            repeatRows=1,
            style=[
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4a4a4a")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                ("ALIGN", (5, 1), (6, -1), "RIGHT"),
            ],
        )

    story = [
        p(po.customer_name, HEADING),
        p(f"{po.ship_to_address} · Einkauf", SMALL),
        Spacer(1, 8),
        p("BESTELLSCHEIN", TITLE),
        fields,
        Spacer(1, 10),
        addresses,
        Spacer(1, 10),
        p(
            "Wir bestellen zu unseren unten angeführten Allgemeinen Einkaufsbedingungen:"
        ),
        Spacer(1, 6),
    ]
    pages, carried = split_lines(po), None
    for n, part in enumerate(pages):
        subtotal = (carried or Decimal(0)) + sum(line.line_total for line in part)
        last = n == len(pages) - 1
        if n:
            story += [
                PageBreak(),
                p(f"Bestellschein {po.po_number}, Fortsetzung", SMALL),
                Spacer(1, 6),
            ]
        story.append(items(part, carried, None if last else subtotal))
        carried = subtotal
    story += [
        Table(
            [["Gesamtbetrag netto EUR", fmt_number(po.total_amount, nf)]],
            colWidths=[429, 76],
            style=[
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ],
        ),
        p("Die Umsatzsteuer ist gesondert auszuweisen.", SMALL),
        Spacer(1, 14),
        p("Allgemeine Einkaufsbedingungen (Auszug)", HEADING),
        *[p(term, SMALL) for term in TERMS_DE],
    ]
    footer = [
        f"{po.customer_name} · Sitz {city} · {x['register']} · UID {x['vat_id']}",
        f"Bankverbindung: IBAN {x['iban']} · BIC {x['bic']}",
    ]
    build_pdf(
        path,
        story,
        margin=45,
        canvasmaker=footer_canvas(footer, "Seite {page} von {total}"),
    )


RENDERERS = {
    "table": layout_table,
    "letter": layout_letter,
    "two_column": layout_two_column,
    "compact": layout_compact,
    "german": layout_german,
    "landscape": layout_landscape,
    "portal": layout_portal,
    "formular": layout_formular,
}


# --- main -------------------------------------------------------------------


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def generate(
    out_dir: Path = DATA_DIR,
    seed: int = SEED,
    count: int = NUM_POS,
    hard: bool = False,
    as_dataset: bool = False,
) -> None:
    """Write PDFs, ground truth and labels. `as_dataset` (an ML dataset outside poc/data)
    writes manifest.json instead of a copy of master_data.json."""
    rng = random.Random(seed)
    pos_dir = out_dir / "pos"
    gt_dir = out_dir / "ground_truth"
    pos_dir.mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    if not as_dataset:
        write_json(out_dir / "master_data.json", master_data())

    orders, labels = build_orders(rng, count, count // 5, hard)
    for order in orders:
        RENDERERS[order.layout.name](order, pos_dir / f"{order.po_id}.pdf")
        (gt_dir / f"{order.po_id}.json").write_text(
            order.po.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    write_json(out_dir / "labels.json", labels)
    if as_dataset:
        write_json(
            out_dir / "manifest.json",
            {
                "seed": seed,
                "count": count,
                "hard": hard,
                "layouts": {order.po_id: order.layout.name for order in orders},
            },
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic purchase orders.")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--count", type=int, default=NUM_POS)
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--hard", action="store_true", help="new layouts, two pages, noise"
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    generate(
        out, args.seed, args.count, args.hard, as_dataset=out != DATA_DIR.resolve()
    )
    print(f"Wrote {args.count} purchase orders to {out}")


if __name__ == "__main__":
    main()
