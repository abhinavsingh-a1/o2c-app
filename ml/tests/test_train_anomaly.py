from datetime import date, datetime
from decimal import Decimal

import pytest

pytest.importorskip("sklearn")  # agent dependencies
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
    printed = capsys.readouterr().out
    assert "25 completed order lines, 1 labelled outliers" in printed
    assert "precision" in printed and "F1" in printed
