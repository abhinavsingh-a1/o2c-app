# E5 Execution Guide — ERP Mock and Data Model

How the SAP-style ERP mock of the Order-to-Cash Agent was built, in the order it was done, with the **final working version** of every file. Problems hit along the way and their fixes are in **Troubleshooting** at the end. Everything runs locally (Docker Compose and kind); no AWS is needed.

**Result of E5:** a Postgres database `erp` with SAP-style tables, seeded master data and 12 months of order history; a FastAPI REST API with idempotent order creation and cancellation; the service running in Docker Compose and in the kind cluster, with its database password passed through a Kubernetes Secret instead of Git.

| SAP table | Our table | Contents |
| --- | --- | --- |
| KNA1 | `kna1` | Customers |
| MARA | `mara` | Materials |
| KONP (simplified) | `prcd` | Prices: list price (`kunnr` NULL) or customer-specific |
| VBAK / VBAP | `vbak` / `vbap` | Sales order header / items |

## Step 1 — Service skeleton, database and models

```powershell
git checkout -b feat/erp-mock
uv init --package services/erp-mock
uv add --package erp-mock fastapi uvicorn sqlalchemy "psycopg[binary]" alembic pydantic-settings
```

`CREATE DATABASE erp;` was added as the last line of `local/postgres-init/01-init.sql`.

`.env` (real password) and `.env.example` (placeholder only):

```
ERP_DATABASE_URL=postgresql+psycopg://o2c:replace-me@localhost:5432/erp
```

**`services/erp-mock/src/erp_mock/db.py`**:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    erp_database_url: str


settings = Settings()
engine = create_engine(settings.erp_database_url)
SessionLocal = sessionmaker(bind=engine)
```

**`services/erp-mock/src/erp_mock/models.py`**:

```python
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):  # SAP KNA1
    __tablename__ = "kna1"
    kunnr: Mapped[str] = mapped_column(String(10), primary_key=True)
    name1: Mapped[str] = mapped_column(String(80))
    stras: Mapped[str] = mapped_column(String(80))
    pstlz: Mapped[str] = mapped_column(String(10))
    ort01: Mapped[str] = mapped_column(String(40))
    land1: Mapped[str] = mapped_column(String(3))
    waers: Mapped[str] = mapped_column(String(3), default="EUR")


class Material(Base):  # SAP MARA
    __tablename__ = "mara"
    matnr: Mapped[str] = mapped_column(String(18), primary_key=True)
    maktx: Mapped[str] = mapped_column(String(120))
    meins: Mapped[str] = mapped_column(String(3))


class Price(Base):  # simplified SAP KONP: kunnr NULL = list price
    __tablename__ = "prcd"
    id: Mapped[int] = mapped_column(primary_key=True)
    matnr: Mapped[str] = mapped_column(ForeignKey("mara.matnr"))
    kunnr: Mapped[str | None] = mapped_column(ForeignKey("kna1.kunnr"))
    kbetr: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    datab: Mapped[date]
    datbi: Mapped[date]


class SalesOrder(Base):  # SAP VBAK
    __tablename__ = "vbak"
    vbeln: Mapped[str] = mapped_column(String(10), primary_key=True)
    kunnr: Mapped[str] = mapped_column(ForeignKey("kna1.kunnr"))
    bstnk: Mapped[str] = mapped_column(String(35))
    bstdk: Mapped[date]
    erdat: Mapped[datetime]
    waerk: Mapped[str] = mapped_column(String(3))
    netwr: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(12), default="OPEN")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    items: Mapped[list["SalesOrderItem"]] = relationship(back_populates="order")


class SalesOrderItem(Base):  # SAP VBAP
    __tablename__ = "vbap"
    __table_args__ = (UniqueConstraint("vbeln", "posnr"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vbeln: Mapped[str] = mapped_column(ForeignKey("vbak.vbeln"))
    posnr: Mapped[int]
    matnr: Mapped[str] = mapped_column(ForeignKey("mara.matnr"))
    arktx: Mapped[str] = mapped_column(String(120))
    kwmeng: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    vrkme: Mapped[str] = mapped_column(String(3))
    netpr: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    netwr: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    order: Mapped[SalesOrder] = relationship(back_populates="items")
```

The unique `idempotency_key` means the database itself refuses a second order for the same PO.

## Step 2 — Alembic migrations

```powershell
uv run alembic init services/erp-mock/migrations
Move-Item alembic.ini services/erp-mock/alembic.ini
```

In `services/erp-mock/alembic.ini`:

```ini
script_location = %(here)s/migrations
```

In `services/erp-mock/migrations/env.py`, replace `target_metadata = None` with:

```python
from erp_mock.db import settings
from erp_mock.models import Base

config.set_main_option("sqlalchemy.url", settings.erp_database_url.replace("%", "%%"))
target_metadata = Base.metadata
```

```powershell
uv run alembic -c services/erp-mock/alembic.ini revision --autogenerate -m "create erp tables"
uv run alembic -c services/erp-mock/alembic.ini upgrade head
docker compose exec postgres psql -U o2c -d erp -c "\dt"
```

Always read the generated migration before applying it.

## Step 3 — Seed master data

Claude Code wrote `erp_mock/seed.py` from this prompt:

```
Create services/erp-mock/src/erp_mock/seed.py.
It loads poc/data/master_data.json and fills kna1 (address split into stras, pstlz, ort01, land1 with ISO codes),
mara, and prcd with one list price per material (kunnr NULL, valid 2026-01-01 to 2026-12-31).
Idempotent; function seed(session, path) and CLI uv run python -m erp_mock.seed using SessionLocal;
print row counts; tests with in-memory SQLite.
```

```powershell
uv run python -m erp_mock.seed
uv run python -m erp_mock.seed
```

Both runs report the same counts (8 customers, 25 materials, 25 prices).

## Step 4 — REST API

| Endpoint | Purpose |
| --- | --- |
| `GET /customers/{kunnr}`, `GET /customers?name=` | Customer lookup |
| `GET /materials/{matnr}`, `GET /materials?q=` | Material lookup, also by description |
| `GET /prices?matnr=&kunnr=&on=` | Customer price first, else list price |
| `POST /salesorders` | Create order; same idempotency key returns the existing order (200) |
| `GET /salesorders/{vbeln}` | Read an order |
| `POST /salesorders/{vbeln}/cancel` | Saga compensation |

**`services/erp-mock/src/erp_mock/schemas.py`**:

```python
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
    source: str


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
```

**`services/erp-mock/src/erp_mock/api.py`** (final version, using the `Annotated` dependency style that satisfies ruff B008):

```python
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
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
    matnr: str, session: SessionDep, kunnr: str | None = None, on: date | None = None
):
    on = on or date.today()
    stmt = select(Price).where(
        Price.matnr == matnr, Price.datab <= on, Price.datbi >= on
    )
    if kunnr:
        stmt = stmt.where((Price.kunnr == kunnr) | Price.kunnr.is_(None))
    else:
        stmt = stmt.where(Price.kunnr.is_(None))
    price = session.scalars(stmt.order_by(Price.kunnr.is_(None))).first()
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
        response.status_code = 200
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
    except IntegrityError:
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
    order.status = "CANCELLED"
    session.commit()
    return _order_by(session, vbeln=vbeln)
```

Design points: idempotency is checked by lookup and by the unique constraint (simultaneous duplicates); items are numbered 10, 20, 30 (SAP style); the API computes all totals.

Try it with `make erp-api` and http://localhost:8100/docs: the same `POST /salesorders` body sent twice returns 201, then 200 with the same `vbeln`. API tests (`tests/test_api.py`, `tests/conftest.py`) were written by Claude Code using in-memory SQLite with `StaticPool` and an overridden `get_session`.

## Step 5 — Order history

Claude Code wrote `erp_mock/seed_history.py`: 12 months (2025-10-01 to 2026-09-30), each customer buying a fixed subset of materials with its own quantity range, 4–8 orders per month, list prices, status COMPLETED, \~2% labelled outliers written to `poc/data/history_outliers.json`, fixed seed, deterministic keys `hist-<kunnr>-<n>`, tests with SQLite.

```powershell
uv run python -m erp_mock.seed_history
uv run python -m erp_mock.seed_history
docker compose exec postgres psql -U o2c -d erp -c "SELECT matnr, count(*) AS lines, round(avg(kwmeng), 1) AS avg_qty, max(kwmeng) AS max_qty FROM vbap GROUP BY matnr ORDER BY max_qty DESC LIMIT 8;"
```

## Step 6 — Container, Compose and kind

**`services/erp-mock/Dockerfile`**:

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package erp-mock

RUN useradd --create-home --uid 10001 app
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8100
CMD ["sh", "-c", "alembic -c services/erp-mock/alembic.ini upgrade head && uvicorn erp_mock.api:app --host 0.0.0.0 --port 8100"]
```

Compose service (database host is `postgres` inside Docker):

```yaml
  erp-mock:
    build:
      context: .
      dockerfile: services/erp-mock/Dockerfile
    environment:
      ERP_DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/erp
    ports:
      - "8100:8100"
    depends_on:
      postgres:
        condition: service_healthy
```

**Shared chart change (o2c-infra, chart version 0.2.0)** — after the `resources:` block in `deployment.yaml`, and `secretName: ""` in `values.yaml`:

```yaml
          {{- with .Values.secretName }}
          envFrom:
            - secretRef:
                name: {{ . }}
          {{- end }}
```

Verify: `helm template test helm/o2c-service --set secretName=my-secret` shows `envFrom`; without it, no `envFrom`.

**`o2c-gitops/envs/kind/erp-mock.yaml`** (no password, only the secret's name):

```yaml
image:
  repository: o2c-erp-mock
  tag: "0.1.0"
containerPort: 8100
healthPath: /health
secretName: erp-mock-secrets
```

Deploy to kind (pods reach the PC's Postgres via `host.docker.internal`):

```bash
docker build -f services/erp-mock/Dockerfile -t o2c-erp-mock:0.1.0 .
kind load docker-image o2c-erp-mock:0.1.0 --name o2c
kubectl create namespace o2c
kubectl create secret generic erp-mock-secrets -n o2c \
  --from-literal=ERP_DATABASE_URL="postgresql+psycopg://o2c:<password>@host.docker.internal:5432/erp"
cd ..
helm install erp-mock o2c-infra/helm/o2c-service -n o2c -f o2c-gitops/envs/kind/erp-mock.yaml
kubectl get pods -n o2c
kubectl port-forward -n o2c svc/erp-mock 18100:80
```

http://localhost:18100/docs → `GET /customers` returns the 8 customers.

**Makefile targets added in E5:** `erp-migrate`, `erp-seed`, `erp-seed-history`, `erp-api`.

## Troubleshooting — problems hit during E5 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | Real passwords and email found in committed `.env.example` (public repo) | Values copied into the example file | Placeholders only in `.env.example`; rotated all passwords in `.env`; `make clean` + `make up`; checked Git history for the OpenAI key (none). gitleaks cannot detect ordinary passwords |
| 2 | `ERROR: database "erp" already exists` | `01-init.sql` already created it on the fresh start | Harmless |
| 3 | `make sync`: *failed to remove … .pyd: Access is denied* | Jupyter was running while sync tried to uninstall the `poc` group | Stop Jupyter/uvicorn; `make sync` now runs `uv sync --all-packages --all-groups` |
| 4 | Alembic: *password authentication failed for user "o2c"* | `ERP_DATABASE_URL` still contained the example password | Password in the URL must equal `POSTGRES_PASSWORD`; compare with `docker compose exec postgres printenv POSTGRES_PASSWORD` |
| 5 | `alembic.ini` created in the repo root | `alembic init` writes the ini to the current folder | Move it into the service folder; `script_location = %(here)s/migrations` |
| 6 | PR merged without the latest work; `gh pr merge`: *local changes would be overwritten* | Pre-commit hooks fixed files, which aborted the commit | `git stash -u` → main → new branch → `git stash pop` → commit until it ends with `[branch …]`; run `make lint` before committing |
| 7 | ruff `B008 Do not perform function call Depends in argument defaults` | Classic FastAPI `= Depends(...)` style | `SessionDep = Annotated[Session, Depends(get_session)]`; session parameter placed before parameters with defaults |
| 8 | `kind load` shows *loading…* with no further output | Copying the image into the cluster takes time | Wait for the prompt to return |
| 9 | Port 8100 already in use | `make erp-api` and the Compose container both use 8100 | Run only one at a time |

**Lessons:**

- `.env.example` only ever contains `replace-me` values.
- Secrets reach Kubernetes as Secrets referenced by name; Git holds only the name.
- Idempotency is enforced in the database, not only in code.
- Check every commit's output before pushing.
