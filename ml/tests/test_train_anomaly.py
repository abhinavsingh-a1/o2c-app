import json
from datetime import date, datetime
from decimal import Decimal

import pytest

pytest.importorskip("sklearn")  # agent dependencies
import numpy as np
import pandas as pd
import train_anomaly
from erp_mock.models import Base, SalesOrder, SalesOrderItem
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from train_anomaly import load_history, outlier_mask


def order(
    vbeln: str, kunnr: str, status: str, items: list[tuple[int, str, str]]
) -> SalesOrder:
    return SalesOrder(
        vbeln=vbeln,
        kunnr=kunnr,
        bstnk=f"PO-{vbeln}",
        bstdk=date(2026, 1, 5),
        erdat=datetime(2026, 1, 5, 9, 0),
        waerk="EUR",
        netwr=Decimal("1.00"),
        status=status,
        idempotency_key=f"key-{vbeln}",
        items=[
            SalesOrderItem(
                posnr=posnr,
                matnr=matnr,
                arktx="x",
                kwmeng=Decimal(qty),
                vrkme="PC",
                netpr=Decimal("1.00"),
                netwr=Decimal("1.00"),
            )
            for posnr, matnr, qty in items
        ],
    )


@pytest.fixture
def erp_url(tmp_path) -> str:
    """SQLite ERP with completed orders (12 x the same two lines), one outlier and an open order."""
    url = f"sqlite:///{(tmp_path / 'erp.db').as_posix()}"
    engine = create_engine(url)
    # SQLite does not enforce the kna1/mara foreign keys, so no master data is needed.
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for n in range(12):
            session.add(
                order(
                    f"H{n:04d}",
                    "1000101",
                    "COMPLETED",
                    [(10, "400101", str(10 + n % 3)), (20, "400102", "5")],
                )
            )
        session.add(order("H9000", "1000101", "COMPLETED", [(10, "400101", "250")]))
        session.add(order("O0001", "1000101", "OPEN", [(10, "400101", "999")]))
        session.commit()
    return url


def test_load_history_reads_only_completed_orders(erp_url):
    history = load_history(create_engine(erp_url))

    assert list(history.columns) == [
        "vbeln",
        "posnr",
        "customer_number",
        "material_number",
        "quantity",
    ]
    assert len(history) == 12 * 2 + 1
    assert "O0001" not in set(history["vbeln"])
    assert history.loc[history["vbeln"] == "H9000", "quantity"].item() == 250.0


def test_outlier_mask_matches_vbeln_and_posnr(erp_url):
    history = load_history(create_engine(erp_url))

    mask = outlier_mask(history, [{"vbeln": "H9000", "posnr": 10, "reason": "25x"}])

    assert mask.sum() == 1
    assert history[mask]["vbeln"].item() == "H9000"


def test_main_trains_and_prints_metrics_without_mlflow(
    erp_url, tmp_path, monkeypatch, capsys
):
    outliers = tmp_path / "outliers.json"
    outliers.write_text(
        '[{"vbeln": "H9000", "posnr": 10, "reason": "25x"}]', encoding="utf-8"
    )
    monkeypatch.setenv("ERP_DATABASE_URL", erp_url)
    monkeypatch.setattr(train_anomaly, "OUTLIERS", outliers)

    metrics = train_anomaly.main(["--no-mlflow"])

    assert metrics["lines"] == 25 and metrics["outliers"] == 1
    assert metrics["recall"] == 1.0  # the 250 among ~10s is found at the 0.8 threshold
    assert set(metrics["false_alarms"]) == {"poc", "holdout", "combined"}
    printed = capsys.readouterr().out
    assert "25 completed order lines, 1 labelled outliers" in printed
    assert "precision" in printed and "F1" in printed
    assert "false alarms on normal PO lines" in printed and "targets:" in printed


# --- false-alarm rate on normal PO lines -----------------------------------

MASTER = json.loads(train_anomaly.MASTER.read_text(encoding="utf-8"))


def test_load_po_lines_maps_names_to_numbers():
    lines = train_anomaly.load_po_lines(train_anomaly.FAR_DATASETS["poc"], MASTER)

    assert len(lines) == 239 and lines["po"].nunique() == 50
    first = lines.iloc[0]
    assert (first.po, first.line_no, first.customer_number) == ("PO-0001", 1, "1000107")
    assert first.material_number == "400117"  # "Keilriemen SPZ 1250", no number printed
    assert (
        lines["customer_number"].notna().all()
        and lines["material_number"].notna().all()
    )


def test_false_alarms_counts_lines_pos_and_causes():
    lines = pd.DataFrame(
        {
            "po": ["A", "A", "B", "C"],
            "customer_number": ["1", "1", "1", "2"],
            "material_number": ["m1", "m2", "m1", "m9"],
            "quantity": [1.0, 2.0, 3.0, 4.0],
        }
    )
    stats = {("1", "m1"): {}, ("1", "m2"): {}}  # ("2", "m9") never ordered

    far = train_anomaly.false_alarms(lines, np.array([0.9, 0.1, 0.5, 0.95]), stats)

    assert far == {
        "lines": 4,
        "flagged_lines": 2,
        "line_rate": 0.5,
        "pos": 3,
        "flagged_pos": 2,
        "po_rate": 2 / 3,
        "flagged_never_ordered": 1,
        "flagged_quantity": 1,
    }


def test_evaluate_false_alarms_per_dataset_and_combined():
    calls = []

    def score(lines):
        calls.append(len(lines))
        return np.where(lines["quantity"] > 1000, 0.99, 0.01)

    far = train_anomaly.evaluate_false_alarms(score, stats={})

    assert list(far) == ["poc", "holdout", "combined"]
    assert calls == [239, 158]
    assert far["combined"]["pos"] == 70 and far["combined"]["lines"] == 397
    assert (
        far["combined"]["flagged_lines"]
        == far["poc"]["flagged_lines"] + far["holdout"]["flagged_lines"]
    )


def test_far_metrics_and_format():
    far = {
        "poc": {
            "po_rate": 0.04,
            "line_rate": 0.01,
            "flagged_pos": 2,
            "pos": 50,
            "flagged_lines": 2,
            "lines": 239,
            "flagged_never_ordered": 0,
            "flagged_quantity": 2,
        }
    }

    assert train_anomaly.far_metrics(far) == {
        "false_alarm_po_rate_poc": 0.04,
        "false_alarm_line_rate_poc": 0.01,
    }
    assert train_anomaly.format_false_alarms(far) == (
        "poc: 2/50 POs (4.0%), 2/239 lines (1.0%; 0 never ordered, 2 quantity)"
    )


# --- promotion rule (also used by the Kubeflow pipeline) ---------------------


def evaluation(recall: float, po_rate: float) -> dict:
    return {
        "history": {"recall": recall},
        "false_alarms": {"combined": {"po_rate": po_rate}},
    }


def champion(po_rate: float) -> dict:
    return {"combined": {"po_rate": po_rate}}


def test_promote_when_better_than_the_champion():
    promoted, reason = train_anomaly.should_promote(
        evaluation(0.90, 0.029), champion(0.929)
    )
    assert promoted
    assert "2.9% < champion 92.9%" in reason


def test_do_not_promote_when_worse_than_the_champion():
    promoted, reason = train_anomaly.should_promote(
        evaluation(0.95, 0.10), champion(0.029)
    )
    assert not promoted
    assert "not below champion" in reason


def test_do_not_promote_a_tie():
    assert not train_anomaly.should_promote(evaluation(0.95, 0.029), champion(0.029))[0]


def test_do_not_promote_when_recall_is_too_low():
    # Fewer false alarms do not help if the model stops finding outliers.
    promoted, reason = train_anomaly.should_promote(
        evaluation(0.79, 0.0), champion(0.929)
    )
    assert not promoted
    assert "recall 79.0% is below 80%" in reason
    assert train_anomaly.should_promote(evaluation(0.80, 0.0), champion(0.929))[0]


def test_first_version_is_promoted_if_recall_is_high_enough():
    assert train_anomaly.should_promote(evaluation(0.85, 0.5), None) == (
        True,
        "there is no champion yet",
    )
    assert not train_anomaly.should_promote(evaluation(0.50, 0.0), None)[0]


# --- pipeline steps: files between components -------------------------------


def test_history_csv_round_trip_keeps_types(erp_url, tmp_path, monkeypatch):
    outliers = tmp_path / "outliers.json"
    outliers.write_text(
        '[{"vbeln": "H9000", "posnr": 10, "reason": "25x"}]', encoding="utf-8"
    )
    history = train_anomaly.load_labelled_history(erp_url, outliers)

    train_anomaly.write_history(history, tmp_path / "history.csv")
    again = train_anomaly.read_history(tmp_path / "history.csv")

    assert list(again.columns) == train_anomaly.HISTORY_COLUMNS
    assert again["customer_number"].iloc[0] == "1000101"  # still a string
    assert again["is_outlier"].dtype == bool and again["is_outlier"].sum() == 1
    pd.testing.assert_frame_equal(again, history[train_anomaly.HISTORY_COLUMNS])


def test_fit_evaluate_and_model_file(erp_url, tmp_path):
    outliers = tmp_path / "outliers.json"
    outliers.write_text(
        '[{"vbeln": "H9000", "posnr": 10, "reason": "25x"}]', encoding="utf-8"
    )
    history = train_anomaly.load_labelled_history(erp_url, outliers)

    model, train_metrics = train_anomaly.fit(history)
    train_anomaly.save_model(model, tmp_path / "model.pkl")
    loaded = train_anomaly.load_model(tmp_path / "model.pkl")
    result = train_anomaly.evaluate(loaded, history)

    # evaluate recomputes exactly the metrics that training reported
    for key in ("precision", "recall", "f1", "tp", "fp", "fn", "lines", "outliers"):
        assert result["history"][key] == train_metrics[key]
    assert result["history"]["raw_cutoff"] == pytest.approx(train_metrics["raw_cutoff"])
    assert set(result["false_alarms"]) == {"poc", "holdout", "combined"}
