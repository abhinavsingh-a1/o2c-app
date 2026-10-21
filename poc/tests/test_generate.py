import json
import random
from datetime import date
from decimal import Decimal

from generate_pos import (
    CUSTOMERS,
    DATA_DIR,
    HARD_LAYOUTS,
    LONG_ORDER_LINES,
    address_parts,
    build_pdf,
    fmt_date,
    fmt_number,
    footer_canvas,
    generate,
    main,
    noise_extras,
    p,
    split_lines,
)
from pypdf import PdfReader
from reportlab.platypus import PageBreak
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


def test_fmt_date_long_de():
    assert fmt_date(date(2026, 1, 5), "long_de") == "5. Jänner 2026"
    assert fmt_date(date(2026, 10, 15), "long_de") == "15. Oktober 2026"


def read_normalised(path) -> bytes:
    """File bytes; text files with LF, because line endings depend on the OS and checkout."""
    data = path.read_bytes()
    return data if path.suffix == ".pdf" else data.replace(b"\r\n", b"\n")


def test_default_options_reproduce_poc_data(tmp_path):
    generate(tmp_path)
    files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.*"))
    assert len(files) == 102
    for f in files:
        assert read_normalised(tmp_path / f) == read_normalised(DATA_DIR / f), f


def test_main_writes_a_dataset_with_manifest(tmp_path, capsys):
    main(["--seed", "7", "--count", "10", "--out", str(tmp_path)])

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["seed"] == 7 and manifest["count"] == 10 and manifest["hard"] is False
    )
    assert len(list((tmp_path / "pos").glob("PO-*.pdf"))) == 10
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    assert sum(label["price_deviation"] for label in labels.values()) == 2
    assert not (tmp_path / "master_data.json").exists()  # stays in poc/data
    assert "Wrote 10 purchase orders" in capsys.readouterr().out


def test_hard_orders_use_new_layouts_and_long_ones_span_two_pages(tmp_path):
    generate(tmp_path, seed=11, count=6, hard=True, as_dataset=True)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["layouts"].values()) <= set(HARD_LAYOUTS)
    long_orders = 0
    for po_id in manifest["layouts"]:
        po = PurchaseOrder.model_validate_json(
            (tmp_path / "ground_truth" / f"{po_id}.json").read_text(encoding="utf-8")
        )
        pages = PdfReader(tmp_path / "pos" / f"{po_id}.pdf").pages
        is_long = len(po.lines) >= LONG_ORDER_LINES
        long_orders += is_long
        assert len(pages) == (2 if is_long else 1), po_id
        text = "\n".join(page.extract_text() for page in pages)
        assert "IBAN" in text, po_id
    assert long_orders == 3


def test_hard_generation_is_deterministic(tmp_path):
    generate(tmp_path / "a", seed=3, count=4, hard=True, as_dataset=True)
    generate(tmp_path / "b", seed=3, count=4, hard=True, as_dataset=True)
    files = sorted(f.relative_to(tmp_path / "a") for f in (tmp_path / "a").rglob("*.*"))
    assert len(files) == 10  # 4 PDFs, 4 ground truths, labels, manifest
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f


def test_address_parts():
    assert address_parts("Brunner Straße 88, 1230 Wien, Austria") == (
        "Brunner Straße 88",
        "1230 Wien",
        "Austria",
    )


def test_split_lines_only_splits_long_orders():
    po = PurchaseOrder.model_validate_json(
        (DATA_DIR / "ground_truth" / "PO-0001.json").read_text(encoding="utf-8")
    )
    assert split_lines(po) == [po.lines]
    lines = [po.lines[0].model_copy(update={"line_no": n}) for n in range(1, 10)]
    first, second = split_lines(po.model_copy(update={"lines": lines}))
    assert [line.line_no for line in first] == [1, 2, 3, 4, 5]
    assert [line.line_no for line in second] == [6, 7, 8, 9]


def test_noise_extras_use_the_customer_country():
    austrian, german = CUSTOMERS[0], CUSTOMERS[4]
    assert noise_extras(random.Random(1), austrian)["iban"].startswith("AT")
    extras = noise_extras(random.Random(1), german)
    assert extras["iban"].startswith("DE")
    assert len(extras["iban"].replace(" ", "")) == 22  # German IBAN length
    assert extras == noise_extras(random.Random(1), german)


def test_footer_canvas_numbers_pages(tmp_path):
    pdf = tmp_path / "two.pdf"
    footer = footer_canvas(["Bank: IBAN AT00 0000"], "Page {page} of {total}")
    build_pdf(pdf, [p("first"), PageBreak(), p("second")], canvasmaker=footer)

    pages = [page.extract_text() for page in PdfReader(pdf).pages]
    assert len(pages) == 2
    assert "Page 1 of 2" in pages[0] and "Page 2 of 2" in pages[1]
    assert all("Bank: IBAN AT00 0000" in page for page in pages)
