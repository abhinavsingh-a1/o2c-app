import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal

import pytest
from erp_mock.models import Base, SalesOrder, SalesOrderItem
from erp_mock.seed import DEFAULT_PATH, seed
from erp_mock.seed_history import (
    DEFAULT_PROFILES_PATH,
    list_prices,
    load_profiles,
    main,
    seed_history,
    weighted_sample,
)
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


def normal_lines(orders, outliers):
    outlier_lines = {(o["vbeln"], o["posnr"]) for o in outliers}
    for order in orders:
        for item in order.items:
            if (order.vbeln, item.posnr) not in outlier_lines:
                yield order.kunnr, item


def test_normal_lines_follow_the_customer_profiles(seeded):
    _, orders, outliers = seeded
    profiles = load_profiles()
    for kunnr, item in normal_lines(orders, outliers):
        assert item.matnr in profiles["customers"][kunnr]["materials"]
        assert float(item.kwmeng) in profiles["materials"][item.matnr]["quantities"]


def test_frequently_ordered_materials_appear_more_often(seeded):
    _, orders, outliers = seeded
    profile = load_profiles()["customers"]["1000108"]["materials"]
    counts = Counter(
        item.matnr
        for kunnr, item in normal_lines(orders, outliers)
        if kunnr == "1000108"
    )
    top = sorted(profile, key=profile.get)[-3:]
    bottom = sorted(profile, key=profile.get)[:3]
    # weights 3..8 here; sampling without replacement flattens them a little
    assert sum(counts[m] for m in top) > 1.5 * sum(counts[m] for m in bottom)


def test_never_bought_outliers_are_outside_the_profile(seeded):
    _, orders, outliers = seeded
    profiles = load_profiles()
    items = {(o.vbeln, i.posnr): (o.kunnr, i) for o in orders for i in o.items}
    never = [o for o in outliers if o["reason"].startswith("material ")]
    assert never  # customers with gaps in their profile get some
    for outlier in never:
        kunnr, item = items[(outlier["vbeln"], outlier["posnr"])]
        assert item.matnr not in profiles["customers"][kunnr]["materials"]
        assert float(item.kwmeng) in profiles["materials"][item.matnr]["quantities"]
    # Only the first purchase of a material is "never bought"; later ones are not.
    pairs = [
        items[(o["vbeln"], o["posnr"])][0]
        + "/"
        + items[(o["vbeln"], o["posnr"])][1].matnr
        for o in never
    ]
    assert len(pairs) == len(set(pairs))


def test_quantity_outliers_are_5_to_20_times_the_median(seeded):
    _, orders, outliers = seeded
    medians = {
        m: Decimal(str(p["median"])) for m, p in load_profiles()["materials"].items()
    }
    items = {(o.vbeln, i.posnr): i for o in orders for i in o.items}
    quantity = [o for o in outliers if o["reason"].startswith("quantity ")]
    assert len(quantity) > len(outliers) / 2  # most customers order every material
    for outlier in quantity:
        item = items[(outlier["vbeln"], outlier["posnr"])]
        assert 4.9 <= item.kwmeng / medians[item.matnr] <= 20.1


def test_reset_replaces_only_the_history(seeded, session, tmp_path):
    counts, _, outliers = seeded
    session.add(
        SalesOrder(
            vbeln="0000000001",
            kunnr="1000101",
            bstnk="PO-1",
            bstdk=date(2026, 10, 1),
            erdat=datetime(2026, 10, 1),
            waerk="EUR",
            netwr=Decimal("1.00"),
            status="OPEN",
            idempotency_key="a" * 64,  # posted by the agent, not history
        )
    )
    session.commit()

    path = tmp_path / "after_reset.json"
    again = seed_history(session, path, reset=True)

    assert again["orders_deleted"] == counts["orders"]
    assert again["orders_created"] == counts["orders"]
    assert (again["orders"], again["lines"]) == (counts["orders"], counts["lines"])
    assert session.get(SalesOrder, "0000000001") is not None
    assert json.loads(path.read_text(encoding="utf-8")) == outliers


def test_missing_profile_is_an_error(session, tmp_path):
    profiles = load_profiles()
    del profiles["customers"]["1000105"]
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles), encoding="utf-8")

    with pytest.raises(ValueError, match="1000105"):
        seed_history(session, tmp_path / "o.json", profiles_path=path)


def test_weighted_sample_draws_distinct_keys_by_weight():
    rng = random.Random(1)
    picks = [weighted_sample(rng, {"a": 90, "b": 9, "c": 1}, 2) for _ in range(500)]
    assert all(len(set(p)) == 2 for p in picks)
    first = Counter(p[0] for p in picks)
    assert first["a"] > first["b"] > first["c"]
    assert weighted_sample(rng, {"a": 1}, 5) == ["a"]


def test_profiles_file_is_committed_and_complete():
    profiles = load_profiles(DEFAULT_PROFILES_PATH)
    assert profiles["purchase_orders"] == 150
    assert len(profiles["customers"]) == 8
    assert len(profiles["materials"]) == 25


def test_main_reset_flag(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        "erp_mock.seed_history.seed_history",
        lambda session, reset: (
            calls.append(reset)
            or {
                "orders_deleted": 3,
                "orders": 3,
                "orders_created": 3,
                "lines": 9,
                "lines_created": 9,
                "outliers": 1,
            }
        ),
    )

    class FakeSessionLocal:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("erp_mock.db.SessionLocal", FakeSessionLocal, raising=False)
    main(["--reset"])

    assert calls == [True]
    assert "Deleted 3 history orders" in capsys.readouterr().out


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
