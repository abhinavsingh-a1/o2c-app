import json
from collections import defaultdict
from datetime import date

import pytest
from erp_mock.models import Base, SalesOrder, SalesOrderItem
from erp_mock.seed import DEFAULT_PATH, seed
from erp_mock.seed_history import list_prices, seed_history
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session, DEFAULT_PATH)
        yield session


@pytest.fixture
def seeded(session, tmp_path):
    path = tmp_path / "history_outliers.json"
    counts = seed_history(session, path)
    orders = session.scalars(
        select(SalesOrder).options(selectinload(SalesOrder.items))
    ).all()
    return counts, orders, json.loads(path.read_text(encoding="utf-8"))


def test_rows_created(seeded, session):
    counts, orders, _ = seeded
    assert counts["orders_created"] == counts["orders"] == len(orders) > 0
    assert (
        counts["lines_created"] == counts["lines"] == sum(len(o.items) for o in orders)
    )

    prices = list_prices(session)
    per_month = defaultdict(int)
    for order in orders:
        assert date(2025, 10, 1) <= order.bstdk <= date(2026, 9, 30)
        assert order.status == "COMPLETED"
        assert order.erdat.date() == order.bstdk
        assert 1 <= len(order.items) <= 6
        assert all(item.netpr == prices[item.matnr] for item in order.items)
        per_month[order.kunnr, order.bstdk.year, order.bstdk.month] += 1
    assert len(per_month) == 8 * 12  # every customer orders every month
    assert all(4 <= n <= 8 for n in per_month.values())


def test_customers_buy_a_fixed_set_of_6_to_10_materials(seeded):
    _, orders, outliers = seeded
    outlier_lines = {(o["vbeln"], o["posnr"]) for o in outliers}
    bought = defaultdict(set)
    for order in orders:
        for item in order.items:
            if (order.vbeln, item.posnr) not in outlier_lines:
                bought[order.kunnr].add(item.matnr)
    assert all(6 <= len(materials) <= 10 for materials in bought.values())


def test_second_run_adds_nothing(seeded, session, tmp_path):
    counts, _, outliers = seeded
    path = tmp_path / "again.json"
    again = seed_history(session, path)
    assert (again["orders_created"], again["lines_created"]) == (0, 0)
    assert (again["orders"], again["lines"]) == (counts["orders"], counts["lines"])
    assert json.loads(path.read_text(encoding="utf-8")) == outliers


def test_outlier_share_between_1_and_3_percent(seeded, session):
    counts, _, outliers = seeded
    assert counts["outliers"] == len(outliers)
    assert 0.01 <= len(outliers) / counts["lines"] <= 0.03
    for outlier in outliers:
        item = session.scalars(
            select(SalesOrderItem).where(
                SalesOrderItem.vbeln == outlier["vbeln"],
                SalesOrderItem.posnr == outlier["posnr"],
            )
        ).one()
        # every outlier points at a real line, and the reason matches that line
        never_bought = (
            f"material {item.matnr} is never bought by customer {item.order.kunnr}"
        )
        assert outlier["reason"] == never_bought or outlier["reason"].startswith(
            f"quantity {int(item.kwmeng)} is "
        )
