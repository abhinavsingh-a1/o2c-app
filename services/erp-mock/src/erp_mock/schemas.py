from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    kunnr: str
    name1: str
    stras: str
    pstlz: str
    ort01: str
    land1: str
    waers: str


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    matnr: str
    maktx: str
    meins: str


class PriceOut(BaseModel):
    matnr: str
    kunnr: str | None
    kbetr: Decimal
    datab: date
    datbi: date
    source: str  # "customer" or "list"


class OrderItemIn(BaseModel):
    matnr: str
    arktx: str
    kwmeng: Decimal = Field(gt=0)
    vrkme: str
    netpr: Decimal = Field(ge=0)


class SalesOrderIn(BaseModel):
    kunnr: str
    bstnk: str
    bstdk: date
    waerk: str = "EUR"
    idempotency_key: str = Field(min_length=16, max_length=64)
    items: list[OrderItemIn] = Field(min_length=1)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    posnr: int
    matnr: str
    arktx: str
    kwmeng: Decimal
    vrkme: str
    netpr: Decimal
    netwr: Decimal


class SalesOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    vbeln: str
    kunnr: str
    bstnk: str
    bstdk: date
    erdat: datetime
    waerk: str
    netwr: Decimal
    status: str
    idempotency_key: str
    items: list[OrderItemOut]
