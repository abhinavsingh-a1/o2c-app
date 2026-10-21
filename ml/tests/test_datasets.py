"""Checks for the generated E10 datasets in ml/data (built with `make ml-data`)."""

import csv
import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

import pytest
from generate_pos import CUSTOMER_PROFILES, HARD_LAYOUTS, LONG_ORDER_LINES
from pypdf import PdfReader
from schema import PurchaseOrder

ROOT = Path(__file__).resolve().parents[2]
ML_DATA = ROOT / "ml" / "data"
MASTER = json.loads(
    (ROOT / "poc" / "data" / "master_data.json").read_text(encoding="utf-8")
)

# dataset -> (number of POs, number of price deviations)
PO_DATASETS = {
    "train": (100, 20),
    "holdout": (20, 4),
    "classifier/train": (60, 12),
    "classifier/train_hard": (60, 12),
    "classifier/holdout": (10, 2),
}
CLASSIFIER_DATASETS = {
    "classifier/train": 60,
    "classifier/train_hard": 60,
    "classifier/holdout": 10,
}
HOLDOUTS = ["holdout", "classifier/holdout"]
DOC_TYPES = {"purchase_order", "invoice", "remittance", "other"}


def printed_in_full(needle: str, layout_text: str) -> bool:
    """True if `needle` is in the text, allowing it to wrap onto the following lines.

    In layout mode a wrapped table cell continues on the next line, while the other
    columns of the row sit between the parts. So each part must appear in one line,
    in order, on consecutive lines.
    """
    words = needle.split()
    lines = [f" {' '.join(line.split())} " for line in layout_text.splitlines()]
    lines = [line for line in lines if line.strip()]

    def match(i: int, k: int) -> bool:  # words[k:] start in lines[i]
        if k == len(words):
            return True
        if i >= len(lines):
            return False
        return any(
            f" {' '.join(words[k:end])} " in lines[i] and match(i + 1, end)
            for end in range(len(words), k, -1)
        )

    return any(match(i, 0) for i in range(len(lines)))


@cache
def ground_truth(dataset: str) -> dict[str, PurchaseOrder]:
    folder = ML_DATA / dataset / "ground_truth"
    return {
        path.stem: PurchaseOrder.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("PO-*.json"))
    }


@cache
def page_texts(pdf: Path) -> tuple[str, ...]:
    return tuple(
        page.extract_text(extraction_mode="layout") for page in PdfReader(pdf).pages
    )


def json_file(dataset: str, name: str) -> dict:
    return json.loads((ML_DATA / dataset / name).read_text(encoding="utf-8"))


# --- the text matcher -------------------------------------------------------


def test_printed_in_full_accepts_one_line_and_wrapped_text():
    text = "1  400101  Sechskantschraube DIN 933   15  PAK  14.90\n   M8x30 verzinkt\n"
    assert printed_in_full("Sechskantschraube DIN 933", text)
    assert printed_in_full("Sechskantschraube DIN 933 M8x30 verzinkt", text)


def test_printed_in_full_rejects_clipped_or_reordered_text():
    text = "1  Sechskantschraube DIN 933   15  PAK\n\n   M8x30\n"
    assert not printed_in_full("Sechskantschraube DIN 933 M8x30 verzinkt", text)
    assert not printed_in_full("DIN 933 Sechskantschraube", text)
    assert not printed_in_full("Schraube", "Sechskantschraube")  # whole words only


# --- purchase orders --------------------------------------------------------


@pytest.mark.parametrize("dataset", PO_DATASETS)
def test_po_counts(dataset):
    count, deviations = PO_DATASETS[dataset]
    ids = [f"PO-{i:04d}" for i in range(1, count + 1)]
    folder = ML_DATA / dataset
    assert sorted(p.stem for p in (folder / "pos").glob("*.pdf")) == ids
    assert sorted(ground_truth(dataset)) == ids
    labels = json_file(dataset, "labels.json")
    assert sorted(labels) == ids
    assert sum(label["price_deviation"] for label in labels.values()) == deviations
    assert sorted(json_file(dataset, "manifest.json")["layouts"]) == ids


@pytest.mark.parametrize("dataset", PO_DATASETS)
def test_totals_add_up(dataset):
    for po_id, po in ground_truth(dataset).items():
        for line in po.lines:
            expected = (line.quantity * line.unit_price).quantize(
                Decimal("0.01"), ROUND_HALF_UP
            )
            assert line.line_total == expected, (po_id, line.line_no)
        assert sum(line.line_total for line in po.lines) == po.total_amount, po_id
        assert [line.line_no for line in po.lines] == list(range(1, len(po.lines) + 1))


@pytest.mark.parametrize("dataset", PO_DATASETS)
def test_customers_and_materials_come_from_master_data(dataset):
    customers = {c["name"]: c for c in MASTER["customers"]}
    materials = {m["description"]: m for m in MASTER["materials"]}
    for po_id, po in ground_truth(dataset).items():
        customer = customers[po.customer_name]
        assert po.ship_to_address == customer["address"], po_id
        assert po.customer_number in (None, customer["customer_number"]), po_id
        for line in po.lines:
            material = materials[line.description]
            assert line.material_number in (None, material["material_number"]), po_id
            assert line.unit == material["unit"], po_id


@pytest.mark.parametrize("dataset", PO_DATASETS)
def test_price_deviations_match_labels(dataset):
    list_prices = {
        m["description"]: Decimal(m["list_price"]) for m in MASTER["materials"]
    }
    labels = json_file(dataset, "labels.json")
    for po_id, po in ground_truth(dataset).items():
        off_list = [
            line.line_no
            for line in po.lines
            if line.unit_price != list_prices[line.description]
        ]
        assert off_list == (
            [labels[po_id]["line_no"]] if labels[po_id]["price_deviation"] else []
        )


@pytest.mark.parametrize("dataset", PO_DATASETS)
def test_descriptions_and_customer_name_are_in_layout_text(dataset):
    for po_id, po in ground_truth(dataset).items():
        text = "\n".join(page_texts(ML_DATA / dataset / "pos" / f"{po_id}.pdf"))
        assert printed_in_full(po.customer_name, text), po_id
        for line in po.lines:
            assert printed_in_full(line.description, text), (po_id, line.line_no)


def test_train_uses_the_six_poc_layouts():
    layouts = set(json_file("train", "manifest.json")["layouts"].values())
    assert layouts == {layout for layout, _ in CUSTOMER_PROFILES.values()}


@pytest.mark.parametrize("dataset", HOLDOUTS)
def test_holdout_uses_only_the_new_layouts(dataset):
    layouts = set(json_file(dataset, "manifest.json")["layouts"].values())
    assert layouts == set(HARD_LAYOUTS)
    assert not layouts & {layout for layout, _ in CUSTOMER_PROFILES.values()}
    # Extraction is trained/tuned without the new layouts; the classifier's
    # train_hard set uses them on purpose (E10 Part C), with its own seeds.
    for other in ("train", "classifier/train"):
        assert not layouts & set(json_file(other, "manifest.json")["layouts"].values())


def test_classifier_train_hard_has_the_holdout_variety():
    layouts = set(
        json_file("classifier/train_hard", "manifest.json")["layouts"].values()
    )
    assert layouts == set(HARD_LAYOUTS)
    texts = [
        "\n".join(page_texts(ML_DATA / "classifier/train_hard" / row["file"]))
        for row in classifier_labels("classifier/train_hard")
    ]
    assert sum("IBAN" in text for text in texts) == len(texts)  # bank-details footer
    assert any("Zahlungserinnerung" in t or "Payment Reminder" in t for t in texts)
    assert any("Auftragsbestätigung" in t or "Order Confirmation" in t for t in texts)


def test_no_training_document_is_a_copy_of_a_holdout_document():
    def pdfs(dataset: str) -> dict[bytes, str]:
        return {p.read_bytes(): str(p) for p in (ML_DATA / dataset).rglob("*.pdf")}

    holdout = {**pdfs("holdout"), **pdfs("classifier/holdout")}
    for dataset in ("train", "classifier/train", "classifier/train_hard"):
        copies = [name for data, name in pdfs(dataset).items() if data in holdout]
        assert copies == [], dataset


def test_holdout_long_orders_span_two_pages_with_repeated_header():
    pos = ground_truth("holdout")
    long_orders = [
        po_id for po_id, po in pos.items() if len(po.lines) >= LONG_ORDER_LINES
    ]
    assert len(long_orders) == len(pos) // 2
    assert all(len(po.lines) <= 14 for po in pos.values())
    for po_id in pos:
        pages = page_texts(ML_DATA / "holdout" / "pos" / f"{po_id}.pdf")
        assert len(pages) == (2 if po_id in long_orders else 1), po_id
        header = "Description" if "Description" in pages[0] else "Bezeichnung"
        assert all(header in page for page in pages), po_id


def test_holdout_contains_noise_text():
    for po_id in ground_truth("holdout"):
        pages = page_texts(ML_DATA / "holdout" / "pos" / f"{po_id}.pdf")
        text = "\n".join(pages)
        assert "IBAN" in text, po_id
        assert "Terms and conditions" in text or "Einkaufsbedingungen" in text, po_id
        for n, page in enumerate(pages, start=1):
            assert (
                f"Page {n} of {len(pages)}" in page
                or f"Seite {n} von {len(pages)}" in page
            )


# --- classifier documents ---------------------------------------------------


@cache
def classifier_labels(dataset: str) -> list[dict]:
    with (ML_DATA / dataset / "labels.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("dataset", CLASSIFIER_DATASETS)
def test_classifier_counts(dataset):
    per_type = CLASSIFIER_DATASETS[dataset]
    rows = classifier_labels(dataset)
    assert list(rows[0]) == ["file", "doc_type"]
    assert Counter(row["doc_type"] for row in rows) == {t: per_type for t in DOC_TYPES}
    files = [row["file"] for row in rows]
    assert len(set(files)) == len(files) == 4 * per_type
    pdfs = sorted(
        p.relative_to(ML_DATA / dataset).as_posix()
        for p in (ML_DATA / dataset).rglob("*.pdf")
    )
    assert pdfs == sorted(files)
    for name in files:
        assert (ML_DATA / dataset / name).read_bytes().startswith(b"%PDF"), name


def test_classifier_holdout_has_confusing_documents():
    texts = [
        "\n".join(page_texts(ML_DATA / "classifier/holdout" / row["file"]))
        for row in classifier_labels("classifier/holdout")
        if row["doc_type"] != "purchase_order"
    ]
    for marker in [
        ("Payment Reminder", "Zahlungserinnerung"),
        ("Payment Notification", "Zahlungsankündigung"),
        ("Request for Quotation", "Anfrage"),
        ("Cancellation of purchase order", "Stornierung unserer Bestellung"),
    ]:
        assert any(m in text for text in texts for m in marker), marker


def test_datasets_use_different_seeds():
    seeds = [json_file(dataset, "manifest.json")["seed"] for dataset in PO_DATASETS]
    assert len(set(seeds)) == len(seeds)
    assert 42 not in seeds  # poc/data


def test_training_seeds_never_reuse_holdout_seeds():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    seeds: dict[str, set[str]] = {"train": set(), "holdout": set()}
    for line in makefile.splitlines():
        if "--seed" in line and "--out ml/data/" in line:
            seed = line.split("--seed")[1].split()[0]
            seeds["holdout" if "holdout" in line else "train"].add(seed)
    assert seeds["holdout"] == {"4242", "5101", "5102"}
    assert not seeds["train"] & seeds["holdout"]
