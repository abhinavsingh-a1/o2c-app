import json
from datetime import date
from decimal import Decimal

from generate_pos import DATA_DIR, fmt_date, fmt_number, generate
from pypdf import PdfReader
from schema import PurchaseOrder

PO_IDS = [f"PO-{i:04d}" for i in range(1, 51)]


def normalise(text: str) -> str:
    return " ".join(text.split())


def test_50_pdfs_and_ground_truth_files_exist():
    for po_id in PO_IDS:
        pdf = DATA_DIR / "pos" / f"{po_id}.pdf"
        assert pdf.read_bytes().startswith(b"%PDF"), pdf
        gt = DATA_DIR / "ground_truth" / f"{po_id}.json"
        PurchaseOrder.model_validate_json(gt.read_text(encoding="utf-8"))
    assert len(list((DATA_DIR / "pos").glob("PO-*.pdf"))) == 50
    assert len(list((DATA_DIR / "ground_truth").glob("PO-*.json"))) == 50


def test_descriptions_and_customer_name_are_printed_in_full():
    for po_id in PO_IDS:
        po = PurchaseOrder.model_validate_json(
            (DATA_DIR / "ground_truth" / f"{po_id}.json").read_text(encoding="utf-8")
        )
        reader = PdfReader(DATA_DIR / "pos" / f"{po_id}.pdf")
        text = normalise(" ".join(page.extract_text() for page in reader.pages))
        assert normalise(po.customer_name) in text, po_id
        for line in po.lines:
            assert normalise(line.description) in text, (po_id, line.line_no)


def test_labels_mark_exactly_10_price_deviations():
    labels = json.loads((DATA_DIR / "labels.json").read_text(encoding="utf-8"))
    assert sorted(labels) == PO_IDS
    assert sum(label["price_deviation"] for label in labels.values()) == 10


def test_generate_is_deterministic(tmp_path):
    generate(tmp_path / "a")
    generate(tmp_path / "b")
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.*"))
    assert len(files) == 102  # 50 PDFs, 50 ground truths, master data, labels
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f


def test_fmt_date():
    d = date(2026, 10, 15)
    assert fmt_date(d, "dmy") == "15.10.2026"
    assert fmt_date(d, "iso") == "2026-10-15"
    assert fmt_date(d, "text") == "15 Oct 2026"


def test_fmt_number():
    assert fmt_number(Decimal("1234.56"), "en") == "1,234.56"
    assert fmt_number(Decimal("1234.56"), "de") == "1.234,56"
