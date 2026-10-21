import json

import pytest
from compare_classifiers import compare, format_table, main


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def results(tmp_path):
    write(
        tmp_path / "classifier-gpt-holdout.json",
        {
            "classifier": "gpt",
            "documents": [{}] * 40,
            "accuracy": 0.975,
            "mistakes": [{"file": "other/DOC-0001.pdf"}],
            "ms_per_document": 1191.7,
            "usage": {"cost_per_1000_documents_usd": 0.3009},
        },
    )
    write(
        tmp_path / "classifier-distilbert-holdout.json",
        {
            "classifier": "distilbert",
            "documents": [{}] * 40,
            "accuracy": 0.95,
            "mistakes": [{}, {}],
            "ms_per_document": 48.2,
        },
    )
    return tmp_path


def test_compare_reads_both_results(results):
    gpt, bert = compare(results)

    assert gpt == {
        "classifier": "GPT (o2c-extract)",
        "documents": 40,
        "accuracy": 0.975,
        "mistakes": 1,
        "ms_per_document": 1191.7,
        "cost_per_1000_documents_usd": 0.3009,
    }
    assert bert["cost_per_1000_documents_usd"] == 0.0  # local CPU model
    assert bert["mistakes"] == 2


def test_gpt_cost_is_unknown_without_token_usage(results):
    path = results / "classifier-gpt-holdout.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["usage"], data["ms_per_document"]
    write(path, data)

    gpt, _ = compare(results)

    assert gpt["cost_per_1000_documents_usd"] is None
    assert "n/a" in format_table([gpt])


def test_format_table(results):
    lines = format_table(compare(results)).splitlines()

    assert lines[0].split() == [
        "classifier",
        "accuracy",
        "mistakes",
        "ms/doc",
        "cost/1000",
        "docs",
    ]
    assert set(lines[1]) <= {"-", " "}
    assert lines[2].split()[-4:] == ["97.5%", "1/40", "1,191.7", "$0.3009"]
    assert lines[3].split()[-4:] == ["95.0%", "2/40", "48.2", "$0.0000"]


def test_main_prints_the_table(results, capsys):
    table = main([str(results)])

    assert "DistilBERT (fine-tuned)" in capsys.readouterr().out
    assert table.count("\n") == 3
