import json
from collections import Counter

from generate_other_docs import generate
from generate_pos import DATA_DIR


def load_doc_types() -> dict:
    return json.loads((DATA_DIR / "doc_types.json").read_text(encoding="utf-8"))


def test_doc_types_has_80_entries():
    assert len(load_doc_types()) == 80


def test_doc_types_counts_and_files_exist():
    doc_types = load_doc_types()
    assert Counter(doc_types.values()) == {
        "purchase_order": 50,
        "invoice": 10,
        "remittance": 10,
        "other": 10,
    }
    for name, doc_type in doc_types.items():
        folder = "pos" if doc_type == "purchase_order" else "other"
        assert (DATA_DIR / folder / name).read_bytes().startswith(b"%PDF"), name


def test_generate_is_deterministic(tmp_path):
    generate(tmp_path / "a")
    generate(tmp_path / "b")
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.*"))
    assert len(files) == 31  # 30 PDFs and doc_types.json
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f
