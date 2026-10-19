from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):  # SAP KNA1
    __tablename__ = "kna1"
    kunnr: Mapped[str] = mapped_column(String(10), primary_key=True)  # customer number
    name1: Mapped[str] = mapped_column(String(80))  # name
    stras: Mapped[str] = mapped_column(String(80))  # street
    pstlz: Mapped[str] = mapped_column(String(10))  # postal code
    ort01: Mapped[str] = mapped_column(String(40))  # city
    land1: Mapped[str] = mapped_column(String(3))  # country
    waers: Mapped[str] = mapped_column(String(3), default="EUR")  # currency


class Material(Base):  # SAP MARA
    __tablename__ = "mara"
    matnr: Mapped[str] = mapped_column(String(18), primary_key=True)  # material number
    maktx: Mapped[str] = mapped_column(String(120))  # description
    meins: Mapped[str] = mapped_column(String(3))  # base unit


class Price(Base):  # simplified SAP KONP: kunnr NULL = list price
    __tablename__ = "prcd"
    id: Mapped[int] = mapped_column(primary_key=True)
    matnr: Mapped[str] = mapped_column(ForeignKey("mara.matnr"))
    kunnr: Mapped[str | None] = mapped_column(ForeignKey("kna1.kunnr"))
    kbetr: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # price
    datab: Mapped[date]  # valid from
    datbi: Mapped[date]  # valid to


class SalesOrder(Base):  # SAP VBAK
    __tablename__ = "vbak"
    vbeln: Mapped[str] = mapped_column(String(10), primary_key=True)  # order number
    kunnr: Mapped[str] = mapped_column(ForeignKey("kna1.kunnr"))
    bstnk: Mapped[str] = mapped_column(String(35))  # customer's PO number
    bstdk: Mapped[date]  # customer's PO date
    erdat: Mapped[datetime]  # created at
    waerk: Mapped[str] = mapped_column(String(3))  # currency
    netwr: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # net value
    status: Mapped[str] = mapped_column(String(12), default="OPEN")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    items: Mapped[list["SalesOrderItem"]] = relationship(back_populates="order")


class SalesOrderItem(Base):  # SAP VBAP
    __tablename__ = "vbap"
    __table_args__ = (UniqueConstraint("vbeln", "posnr"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vbeln: Mapped[str] = mapped_column(ForeignKey("vbak.vbeln"))
    posnr: Mapped[int]  # item number
    matnr: Mapped[str] = mapped_column(ForeignKey("mara.matnr"))
    arktx: Mapped[str] = mapped_column(String(120))  # item description
    kwmeng: Mapped[Decimal] = mapped_column(Numeric(12, 3))  # quantity
    vrkme: Mapped[str] = mapped_column(String(3))  # sales unit
    netpr: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # net price
    netwr: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # net value
    order: Mapped[SalesOrder] = relationship(back_populates="items")
