import csv
import json
import random
from collections import Counter

import generate_pos
from generate_other_docs import generate, kind_counts, main, noise_footer
from generate_pos import DATA_DIR
from pypdf import PdfReader


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


def test_default_options_reproduce_poc_data(tmp_path):
    generate(tmp_path)
    files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.*"))
    assert len(files) == 31
    for f in files:
        got, expected = (tmp_path / f).read_bytes(), (DATA_DIR / f).read_bytes()
        if f.suffix != ".pdf":  # line endings depend on the OS and the git checkout
            got, expected = (
                got.replace(b"\r\n", b"\n"),
                expected.replace(b"\r\n", b"\n"),
            )
        assert got == expected, f


def test_kind_counts_default_is_the_poc_mix():
    assert kind_counts() == {
        "invoice": 10,
        "remittance": 10,
        "confirmation": 4,
        "delivery_note": 3,
        "newsletter": 3,
    }


def test_kind_counts_gives_count_documents_per_type():
    types = {
        "invoice": "invoice",
        "payment_reminder": "invoice",
        "remittance": "remittance",
        "payment_notice": "remittance",
    }
    for count in (7, 10, 60):
        for hard in (False, True):
            per_type = Counter()
            for kind, n in kind_counts(count, hard).items():
                per_type[types.get(kind, "other")] += n
            assert per_type == {"invoice": count, "remittance": count, "other": count}
    assert kind_counts(10, hard=True)["rfq"] == 2


def test_noise_footer_only_with_hard():
    rng = random.Random(1)
    assert noise_footer(rng, False, "X GmbH", "Weg 1, 1010 Wien, Austria", "de") == {}
    assert rng.random() == random.Random(1).random()  # no random draws without --hard
    assert "canvasmaker" in noise_footer(
        rng, True, "X GmbH", "Weg 1, 1010 Wien, Austria", "de"
    )


def test_main_writes_hard_classifier_dataset(tmp_path):
    generate_pos.generate(tmp_path, seed=1, count=5, hard=True, as_dataset=True)

    main(["--seed", "2", "--count", "5", "--hard", "--out", str(tmp_path)])

    with (tmp_path / "labels.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert Counter(row["doc_type"] for row in rows) == {
        "purchase_order": 5,
        "invoice": 5,
        "remittance": 5,
        "other": 5,
    }
    for row in rows:
        reader = PdfReader(tmp_path / row["file"])
        text = reader.pages[0].extract_text()
        total = len(reader.pages)
        assert f"Page 1 of {total}" in text or f"Seite 1 von {total}" in text, row[
            "file"
        ]
