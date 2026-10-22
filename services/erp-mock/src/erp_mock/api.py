from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from o2c_common.metrics import instrument_fastapi
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from erp_mock.db import SessionLocal
from erp_mock.models import Customer, Material, Price, SalesOrder, SalesOrderItem
from erp_mock.schemas import (
    CustomerOut,
    MaterialOut,
    PriceOut,
    SalesOrderIn,
    SalesOrderOut,
)

app = FastAPI(title="ERP mock", version="0.1.0")
instrument_fastapi(app)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _order_by(session: Session, **where) -> SalesOrder | None:
    stmt = select(SalesOrder).options(selectinload(SalesOrder.items)).filter_by(**where)
    return session.scalars(stmt).first()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/customers/{kunnr}", response_model=CustomerOut)
def get_customer(kunnr: str, session: SessionDep):
    customer = session.get(Customer, kunnr)
    if customer is None:
        raise HTTPException(404, "customer not found")
    return customer


@app.get("/customers", response_model=list[CustomerOut])
def search_customers(session: SessionDep, name: str = ""):
    stmt = (
        select(Customer)
        .where(Customer.name1.ilike(f"%{name}%"))
        .order_by(Customer.kunnr)
    )
    return session.scalars(stmt).all()


@app.get("/materials/{matnr}", response_model=MaterialOut)
def get_material(matnr: str, session: SessionDep):
    material = session.get(Material, matnr)
    if material is None:
        raise HTTPException(404, "material not found")
    return material


@app.get("/materials", response_model=list[MaterialOut])
def search_materials(session: SessionDep, q: str = ""):
    stmt = (
        select(Material).where(Material.maktx.ilike(f"%{q}%")).order_by(Material.matnr)
    )
    return session.scalars(stmt).all()


@app.get("/prices", response_model=PriceOut)
def get_price(
    matnr: str,
    session: SessionDep,
    kunnr: str | None = None,
    on: date | None = None,
):
    on = on or date.today()
    stmt = select(Price).where(
        Price.matnr == matnr, Price.datab <= on, Price.datbi >= on
    )
    if kunnr:
        stmt = stmt.where((Price.kunnr == kunnr) | Price.kunnr.is_(None))
    else:
        stmt = stmt.where(Price.kunnr.is_(None))
    price = session.scalars(
        stmt.order_by(Price.kunnr.is_(None))
    ).first()  # customer price first
    if price is None:
        raise HTTPException(404, "no valid price")
    return PriceOut(
        matnr=price.matnr,
        kunnr=price.kunnr,
        kbetr=price.kbetr,
        datab=price.datab,
        datbi=price.datbi,
        source="list" if price.kunnr is None else "customer",
    )


@app.post("/salesorders", response_model=SalesOrderOut, status_code=201)
def create_order(order: SalesOrderIn, response: Response, session: SessionDep):
    existing = _order_by(session, idempotency_key=order.idempotency_key)
    if existing:
        response.status_code = (
            200  # same PO sent again: return the order we already made
        )
        return existing
    if session.get(Customer, order.kunnr) is None:
        raise HTTPException(422, f"unknown customer {order.kunnr}")
    for item in order.items:
        if session.get(Material, item.matnr) is None:
            raise HTTPException(422, f"unknown material {item.matnr}")

    items = [
        SalesOrderItem(
            posnr=10 * (i + 1),
            matnr=it.matnr,
            arktx=it.arktx,
            kwmeng=it.kwmeng,
            vrkme=it.vrkme,
            netpr=it.netpr,
            netwr=(it.kwmeng * it.netpr).quantize(Decimal("0.01")),
        )
        for i, it in enumerate(order.items)
    ]
    sales_order = SalesOrder(
        vbeln=f"{uuid4().int % 10**10:010d}",
        kunnr=order.kunnr,
        bstnk=order.bstnk,
        bstdk=order.bstdk,
        erdat=datetime.now(UTC).replace(tzinfo=None),
        waerk=order.waerk,
        netwr=sum(i.netwr for i in items),
        status="OPEN",
        idempotency_key=order.idempotency_key,
        items=items,
    )
    session.add(sales_order)
    try:
        session.commit()
    except IntegrityError:  # two identical requests at the same moment
        session.rollback()
        existing = _order_by(session, idempotency_key=order.idempotency_key)
        if existing is None:
            raise
        response.status_code = 200
        return existing
    return _order_by(session, vbeln=sales_order.vbeln)


@app.get("/salesorders/{vbeln}", response_model=SalesOrderOut)
def get_order(vbeln: str, session: SessionDep):
    order = _order_by(session, vbeln=vbeln)
    if order is None:
        raise HTTPException(404, "order not found")
    return order


@app.post("/salesorders/{vbeln}/cancel", response_model=SalesOrderOut)
def cancel_order(vbeln: str, session: SessionDep):
    order = _order_by(session, vbeln=vbeln)
    if order is None:
        raise HTTPException(404, "order not found")
    order.status = "CANCELLED"  # cancelling twice is harmless
    session.commit()
    return _order_by(session, vbeln=vbeln)
