# E8 Execution Guide — Validation and Posting Agent (LangGraph)

How the order agent was built, in the order it was done, with the **final working version** of every module. Problems and fixes are in **Troubleshooting** at the end. All commands run in Git Bash from the `o2c-app` folder.

**Result of E8:** an `agent` service consumes `orders.extracted`, validates each PO against the ERP and the customer contract, and either posts the sales order automatically or pauses for a human. Every node is checkpointed in Postgres, so a crash resumes without double posting. People decide through `POST /reviews/{key}` (approve / edit / reject). Outcomes are published to `orders.validated`, `orders.posted` and `orders.exceptions`.

## Architecture

```
resolve_customer → resolve_materials → check_prices → check_anomalies → decide
  decide: no issues and below limit → post_order → verify_order → posted
          verify mismatch → cancel_order (saga compensation) → explain_exception
  decide: issues or above limit → explain_exception → human_review (interrupt)
  human_review: approve → post_order | edit → apply_edit → resolve_customer | reject → rejected
```

| Principle | How it is implemented |
| --- | --- |
| LLM never decides posting | `decide` is a plain rule: any issue or total above 10,000 EUR → human |
| LLM output checked against master data | Material matches accepted only if the number is a real ERP candidate |
| Prices from ERP, wording from RAG | Price via `GET /prices` (SQL); tolerance clause via pgvector |
| Crash safety | Postgres checkpoint after every node; thread id = idempotency key |
| No double posting | ERP `POST /salesorders` is idempotent on the key |
| Saga | `verify_order` reads the order back; mismatch → `cancel_order` |
| Human oversight | `interrupt()` in `human_review`; decisions via API; second decision → 409 |

## Step 1 — Design: ADR-004 and state

```bash
git checkout -b feat/agent-design
uv init --package services/agent
uv add --package agent langgraph pydantic o2c-common
```

`docs/adr/0004-agent-graph.md` records the graph (as a Mermaid diagram, rendered by GitHub), the rules above, the issue codes and the human-review behaviour. View it with `gh browse docs/adr/0004-agent-graph.md`.

**`services/agent/src/agent/state.py`** (final):

```python
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from o2c_common.schema import PurchaseOrder


class IssueCode(StrEnum):
    UNKNOWN_CUSTOMER = "UNKNOWN_CUSTOMER"
    UNKNOWN_MATERIAL = "UNKNOWN_MATERIAL"
    NO_AGREED_PRICE = "NO_AGREED_PRICE"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    QUANTITY_ANOMALY = "QUANTITY_ANOMALY"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    ORDER_VERIFY_FAILED = "ORDER_VERIFY_FAILED"
    ABOVE_AUTO_POST_LIMIT = "ABOVE_AUTO_POST_LIMIT"
    APPROVAL_NOT_POSSIBLE = "APPROVAL_NOT_POSSIBLE"


class Issue(BaseModel):
    code: IssueCode
    message: str
    line_no: int | None = None


class LineCheck(BaseModel):
    line_no: int
    material_number: str | None = None
    material_source: Literal["document", "erp_search", "llm"] | None = None
    agreed_price: Decimal | None = None
    tolerance_pct: Decimal | None = None
    deviation_pct: Decimal | None = None
    anomaly_score: float | None = None


class Review(BaseModel):
    action: Literal["approve", "edit", "reject"]
    reviewer: str
    comment: str | None = None
    edited_po: PurchaseOrder | None = None


class AgentState(BaseModel):
    idempotency_key: str
    correlation_id: str
    source_event_id: str
    purchase_order: PurchaseOrder
    source_document: dict[str, Any] = Field(default_factory=dict)

    customer_number: str | None = None
    lines: list[LineCheck] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    decision: Literal["auto_post", "exception"] | None = None
    explanation: str | None = None
    review: Review | None = None
    sales_order_number: str | None = None
    status: Literal["running", "awaiting_review", "posted", "rejected", "failed"] = (
        "running"
    )
```

Test `tests/test_state.py` checks that a JSON round-trip keeps decimals exact (`9.80`), which the checkpointer depends on.

## Step 2 — Tools

```bash
git checkout -b feat/agent-tools
uv add --package agent httpx pydantic-settings langchain-openai langchain-postgres langchain-text-splitters "psycopg[binary]"
```

**`agent/settings.py`**:

```python
from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    erp_base_url: str = "http://localhost:8100"
    kafka_bootstrap: str = "localhost:9092"
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str
    events_database_url: str
    prompts_dir: Path = REPO_ROOT / "prompts"
    contracts_dir: Path = REPO_ROOT / "poc" / "data" / "contracts"
    auto_post_limit: Decimal = Decimal("10000")
```

**`agent/erp.py`** — 404 → `None` (becomes an Issue), 422 → `ErpRejected` (no retry), 5xx/timeout → exception (retried):

```python
from datetime import date
from typing import Any

import httpx


class ErpRejected(Exception):
    """The ERP refused the data (HTTP 422). Retrying will not help."""


class ErpClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self._http = client or httpx.Client(base_url=base_url, timeout=10)

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = self._http.get(path, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_customer(self, kunnr: str) -> dict | None:
        return self._get(f"/customers/{kunnr}")

    def search_customers(self, name: str) -> list[dict]:
        return self._get("/customers", {"name": name}) or []

    def get_material(self, matnr: str) -> dict | None:
        return self._get(f"/materials/{matnr}")

    def search_materials(self, q: str = "") -> list[dict]:
        return self._get("/materials", {"q": q}) or []

    def get_price(self, matnr: str, kunnr: str, on: date) -> dict | None:
        return self._get(
            "/prices", {"matnr": matnr, "kunnr": kunnr, "on": on.isoformat()}
        )

    def create_order(self, order: dict) -> dict:
        response = self._http.post("/salesorders", json=order)
        if response.status_code == 422:
            raise ErpRejected(response.json().get("detail"))
        response.raise_for_status()
        return response.json()

    def get_order(self, vbeln: str) -> dict | None:
        return self._get(f"/salesorders/{vbeln}")

    def cancel_order(self, vbeln: str) -> dict:
        response = self._http.post(f"/salesorders/{vbeln}/cancel")
        response.raise_for_status()
        return response.json()
```

**`agent/contracts.py`** — the RAG index is rebuilt from scratch on every ingest (no duplicates); the tolerance is cached per customer:

```python
import json
from decimal import Decimal
from pathlib import Path

from langchain_core.documents import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel

from agent.settings import Settings

COLLECTION = "contracts"


class Tolerance(BaseModel):
    tolerance_pct: Decimal | None
    evidence: str


def make_store(settings: Settings) -> PGVector:
    embeddings = OpenAIEmbeddings(
        model="o2c-embed",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        check_embedding_ctx_length=False,
    )
    return PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION,
        connection=settings.events_database_url,
        use_jsonb=True,
    )


def ingest_contracts(store: PGVector, contracts_dir: Path) -> int:
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
    docs = []
    for path in sorted(contracts_dir.glob("*.md")):
        customer_number = path.stem.split("-")[0]
        for chunk in splitter.split_text(path.read_text(encoding="utf-8")):
            docs.append(
                Document(
                    page_content=chunk,
                    metadata={"customer_number": customer_number, "source": path.name},
                )
            )
    store.delete_collection()
    store.create_collection()
    store.add_documents(docs)
    return len(docs)


class ToleranceLookup:
    def __init__(self, store: PGVector, chain, prompts_dir: Path):
        self._store, self._chain = store, chain
        template = (prompts_dir / "tolerance.md").read_text(encoding="utf-8")
        self._prompt = template.replace(
            "{schema}", json.dumps(Tolerance.model_json_schema())
        )
        self._cache: dict[str, Tolerance] = {}

    def __call__(self, customer_number: str) -> Tolerance:
        if customer_number in self._cache:
            return self._cache[customer_number]
        hits = self._store.similarity_search(
            "price tolerance deviation percent",
            k=2,
            filter={"customer_number": customer_number},
        )
        if not hits:
            return Tolerance(tolerance_pct=None, evidence="no contract found")
        context = "\n---\n".join(h.page_content for h in hits)
        result = self._chain.invoke([("system", self._prompt), ("human", context)])
        self._cache[customer_number] = result
        return result


def make_tolerance_lookup(settings: Settings, store: PGVector) -> ToleranceLookup:
    llm = ChatOpenAI(
        model="o2c-extract",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        temperature=0,
        extra_body={"metadata": {"trace_name": "tolerance", "tags": ["agent"]}},
    )
    return ToleranceLookup(
        store,
        llm.with_structured_output(Tolerance, method="json_mode"),
        settings.prompts_dir,
    )


if __name__ == "__main__":
    s = Settings()
    print(f"ingested {ingest_contracts(make_store(s), s.contracts_dir)} chunks")
```

**`agent/materials.py`** — hallucination guard: a number not in the candidate list is replaced by `None`:

```python
import json
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from agent.settings import Settings


class MaterialMatch(BaseModel):
    matnr: str | None
    reason: str


class MaterialMatcher:
    def __init__(self, chain, prompts_dir: Path):
        self._chain = chain
        self._template = (prompts_dir / "match_material.md").read_text(encoding="utf-8")

    def __call__(self, description: str, candidates: list[dict]) -> MaterialMatch:
        listing = "\n".join(
            f"{c['matnr']}: {c['maktx']} ({c['meins']})" for c in candidates
        )
        prompt = self._template.replace("{candidates}", listing).replace(
            "{schema}", json.dumps(MaterialMatch.model_json_schema())
        )
        result = self._chain.invoke([("system", prompt), ("human", description)])
        valid = {c["matnr"] for c in candidates}
        if result.matnr is not None and result.matnr not in valid:
            return MaterialMatch(
                matnr=None, reason=f"rejected invented number {result.matnr}"
            )
        return result


def make_material_matcher(settings: Settings) -> MaterialMatcher:
    llm = ChatOpenAI(
        model="o2c-extract",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        temperature=0,
        extra_body={"metadata": {"trace_name": "match_material", "tags": ["agent"]}},
    )
    return MaterialMatcher(
        llm.with_structured_output(MaterialMatch, method="json_mode"),
        settings.prompts_dir,
    )
```

**`agent/anomaly.py`** — placeholder (always 0.0) until the IsolationForest model in E10.

Prompt files: `prompts/tolerance.md`, `prompts/match_material.md` (with `{candidates}` and `{schema}` placeholders). Smoke test `local/agent_tools_smoke.py` exercises the ERP client, tolerance lookup and material matching against the running stack; `make contracts-ingest` rebuilds the index.

## Step 3 — Graph

Nodes are plain functions returning only changed fields; list fields are returned in full. Materials are resolved in three tries: printed number checked in ERP → ERP text search with exactly one hit → guarded GPT match. `check_prices` also checks PO arithmetic with commercial rounding (`ROUND_HALF_UP`) and uses the price valid on the PO date. The final graph code is in Step 5.

Dry-run evaluation `local/agent_graph_eval.py` runs the graph on all 50 ground-truth POs with a `DryRunErp` (real reads, in-memory writes) and compares `PRICE_DEVIATION` lines with `poc/data/labels.json`. Acceptance criterion: price check correct 50/50, exactly 10 `PRICE_DEVIATION` issues. The graph structure can be printed with `get_graph().draw_mermaid()` and viewed at mermaid.live.

## Step 4 — Checkpointing and crash test

```bash
git checkout -b feat/agent-checkpoint
uv add --package agent langgraph-checkpoint-postgres
```

`local/agent_crash_test.py` simulates two crashes with the real ERP and real Postgres:

| Scenario | Nodes left after crash | 2nd run | create\_order calls in 2nd run |
| --- | --- | --- | --- |
| Crash right after the ERP created the order | `('post_order',)` | posted, same order | 1 (ERP returns the existing order) |
| Crash while verifying | `('verify_order',)` | posted | 0 |
| Third run of a finished order | — | stored result | 0 |

Proof of no duplicate: `SELECT … FROM vbak WHERE idempotency_key = '<key>'` returns exactly one row.

## Step 5 — Human in the loop

`prompts/explain_exception.md` asks for 2–4 plain sentences using only the given facts. **`agent/explain.py`**:

```python
import json

from langchain_openai import ChatOpenAI

from agent.settings import Settings
from agent.state import AgentState


def make_explainer(settings: Settings):
    system_prompt = (settings.prompts_dir / "explain_exception.md").read_text(
        encoding="utf-8"
    )

    def explain(state: AgentState) -> str:
        po = state.purchase_order
        facts = {
            "po_number": po.po_number,
            "customer": po.customer_name,
            "total": str(po.total_amount),
            "currency": po.currency,
            "lines": [
                {
                    "line_no": line.line_no,
                    "description": line.description,
                    "quantity": str(line.quantity),
                    "unit_price": str(line.unit_price),
                }
                for line in po.lines
            ],
            "issues": [issue.message for issue in state.issues],
        }
        llm = ChatOpenAI(
            model="o2c-extract",
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_master_key,
            temperature=0,
            extra_body={
                "metadata": {
                    "trace_name": "explain_exception",
                    "session_id": state.correlation_id,
                    "tags": ["agent"],
                }
            },
        )
        return llm.invoke(
            [
                ("system", system_prompt),
                ("human", json.dumps(facts, ensure_ascii=False)),
            ]
        ).content

    return explain
```

**`agent/graph.py`** (final):

```python
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.contracts import Tolerance
from agent.erp import ErpClient, ErpRejected
from agent.materials import MaterialMatch
from agent.state import AgentState, Issue, IssueCode, LineCheck, Review

CENT = Decimal("0.01")
ANOMALY_THRESHOLD = 0.8


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _can_post(state: AgentState) -> bool:
    return (
        state.customer_number is not None
        and len(state.lines) == len(state.purchase_order.lines)
        and all(check.material_number for check in state.lines)
    )


@dataclass
class Deps:
    erp: ErpClient
    tolerance: Callable[[str], Tolerance]
    match_material: Callable[[str, list[dict]], MaterialMatch]
    anomaly: Callable[[str, str, float], float]
    explain: Callable[[AgentState], str]
    auto_post_limit: Decimal


def build_graph(deps: Deps, checkpointer=None):
    erp = deps.erp

    def resolve_customer(state: AgentState) -> dict:
        po = state.purchase_order
        if po.customer_number and erp.get_customer(po.customer_number):
            return {"customer_number": po.customer_number}
        matches = [
            c
            for c in erp.search_customers(po.customer_name)
            if _norm(c["name1"]) == _norm(po.customer_name)
        ]
        if len(matches) == 1:
            return {"customer_number": matches[0]["kunnr"]}
        return {
            "issues": [
                *state.issues,
                Issue(
                    code=IssueCode.UNKNOWN_CUSTOMER,
                    message=f"Customer '{po.customer_name}' was not found in the ERP.",
                ),
            ]
        }

    def resolve_materials(state: AgentState) -> dict:
        issues, lines, catalogue = list(state.issues), [], None
        for line in state.purchase_order.lines:
            if line.material_number and erp.get_material(line.material_number):
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=line.material_number,
                        material_source="document",
                    )
                )
                continue
            hits = erp.search_materials(line.description)
            if len(hits) == 1:
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=hits[0]["matnr"],
                        material_source="erp_search",
                    )
                )
                continue
            catalogue = catalogue or erp.search_materials("")
            match = deps.match_material(line.description, catalogue)
            if match.matnr:
                lines.append(
                    LineCheck(
                        line_no=line.line_no,
                        material_number=match.matnr,
                        material_source="llm",
                    )
                )
            else:
                lines.append(LineCheck(line_no=line.line_no))
                issues.append(
                    Issue(
                        code=IssueCode.UNKNOWN_MATERIAL,
                        line_no=line.line_no,
                        message=f"Line {line.line_no}: no material found for "
                        f"'{line.description}'.",
                    )
                )
        return {"lines": lines, "issues": issues}

    def check_prices(state: AgentState) -> dict:
        po = state.purchase_order
        issues = list(state.issues)
        for line in po.lines:
            if _money(line.quantity * line.unit_price) != line.line_total:
                issues.append(
                    Issue(
                        code=IssueCode.TOTAL_MISMATCH,
                        line_no=line.line_no,
                        message=f"Line {line.line_no}: {line.quantity} x {line.unit_price} "
                        f"is not {line.line_total}.",
                    )
                )
        if sum((line.line_total for line in po.lines), Decimal(0)) != po.total_amount:
            issues.append(
                Issue(
                    code=IssueCode.TOTAL_MISMATCH,
                    message=f"The line totals do not add up to {po.total_amount}.",
                )
            )
        if state.customer_number is None:
            return {"issues": issues}
        tolerance = deps.tolerance(state.customer_number).tolerance_pct or Decimal(0)
        po_lines = {line.line_no: line for line in po.lines}
        checked = []
        for check in state.lines:
            line = po_lines[check.line_no]
            if check.material_number is None:
                checked.append(check)
                continue
            price = erp.get_price(
                check.material_number, state.customer_number, po.po_date
            )
            if price is None:
                issues.append(
                    Issue(
                        code=IssueCode.NO_AGREED_PRICE,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: no valid price for material "
                        f"{check.material_number} on {po.po_date}.",
                    )
                )
                checked.append(check)
                continue
            agreed = Decimal(str(price["kbetr"]))
            deviation = _money(abs(line.unit_price - agreed) / agreed * 100)
            if deviation > tolerance:
                issues.append(
                    Issue(
                        code=IssueCode.PRICE_DEVIATION,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: price {line.unit_price} vs agreed "
                        f"{agreed} ({deviation}% > tolerance {tolerance}%).",
                    )
                )
            checked.append(
                check.model_copy(
                    update={
                        "agreed_price": agreed,
                        "tolerance_pct": tolerance,
                        "deviation_pct": deviation,
                    }
                )
            )
        return {"lines": checked, "issues": issues}

    def check_anomalies(state: AgentState) -> dict:
        if state.customer_number is None:
            return {}
        issues, checked = list(state.issues), []
        quantities = {
            line.line_no: line.quantity for line in state.purchase_order.lines
        }
        for check in state.lines:
            if check.material_number is None:
                checked.append(check)
                continue
            score = deps.anomaly(
                state.customer_number,
                check.material_number,
                float(quantities[check.line_no]),
            )
            if score > ANOMALY_THRESHOLD:
                issues.append(
                    Issue(
                        code=IssueCode.QUANTITY_ANOMALY,
                        line_no=check.line_no,
                        message=f"Line {check.line_no}: unusual quantity "
                        f"{quantities[check.line_no]} (score {score:.2f}).",
                    )
                )
            checked.append(check.model_copy(update={"anomaly_score": score}))
        return {"lines": checked, "issues": issues}

    def decide(state: AgentState) -> dict:
        issues = list(state.issues)
        total = state.purchase_order.total_amount
        if total > deps.auto_post_limit:
            issues.append(
                Issue(
                    code=IssueCode.ABOVE_AUTO_POST_LIMIT,
                    message=f"Order total {total} is above the auto-post limit "
                    f"{deps.auto_post_limit}.",
                )
            )
        return {"issues": issues, "decision": "exception" if issues else "auto_post"}

    def post_order(state: AgentState) -> dict:
        po = state.purchase_order
        materials = {check.line_no: check.material_number for check in state.lines}
        order = {
            "kunnr": state.customer_number,
            "bstnk": po.po_number,
            "bstdk": po.po_date.isoformat(),
            "waerk": po.currency,
            "idempotency_key": state.idempotency_key,
            "items": [
                {
                    "matnr": materials[line.line_no],
                    "arktx": line.description[:120],
                    "kwmeng": str(line.quantity),
                    "vrkme": line.unit,
                    "netpr": str(line.unit_price),
                }
                for line in po.lines
            ],
        }
        try:
            created = erp.create_order(order)
        except ErpRejected as exc:
            return {
                "issues": [
                    *state.issues,
                    Issue(
                        code=IssueCode.ORDER_VERIFY_FAILED,
                        message=f"The ERP rejected the order: {exc}",
                    ),
                ]
            }
        return {"sales_order_number": created["vbeln"]}

    def verify_order(state: AgentState) -> dict:
        order = erp.get_order(state.sales_order_number)
        po = state.purchase_order
        if (
            order
            and order["status"] == "OPEN"
            and Decimal(str(order["netwr"])) == po.total_amount
            and len(order["items"]) == len(po.lines)
        ):
            return {"status": "posted"}
        return {
            "issues": [
                *state.issues,
                Issue(
                    code=IssueCode.ORDER_VERIFY_FAILED,
                    message=f"Sales order {state.sales_order_number} in the ERP does not match the PO.",
                ),
            ]
        }

    def cancel_order(state: AgentState) -> dict:
        erp.cancel_order(state.sales_order_number)
        return {}

    def explain_exception(state: AgentState) -> dict:
        try:
            text = deps.explain(state)
        except Exception:  # noqa: BLE001
            text = "\n".join(f"- {issue.message}" for issue in state.issues)
        return {"explanation": text, "status": "awaiting_review"}

    def human_review(state: AgentState) -> dict:
        answer = interrupt(
            {
                "idempotency_key": state.idempotency_key,
                "explanation": state.explanation,
                "issues": [issue.model_dump(mode="json") for issue in state.issues],
                "purchase_order": state.purchase_order.model_dump(mode="json"),
            }
        )
        review = Review.model_validate(answer)
        if review.action == "approve" and not _can_post(state):
            return {
                "review": review,
                "issues": [
                    *state.issues,
                    Issue(
                        code=IssueCode.APPROVAL_NOT_POSSIBLE,
                        message="Approve is not possible because the customer or a material is unknown. "
                        "Please edit the order instead.",
                    ),
                ],
            }
        if review.action == "edit" and review.edited_po is None:
            return {
                "review": review,
                "issues": [
                    *state.issues,
                    Issue(
                        code=IssueCode.APPROVAL_NOT_POSSIBLE,
                        message="Edit was chosen, but no corrected order was provided.",
                    ),
                ],
            }
        return {"review": review}

    def apply_edit(state: AgentState) -> dict:
        return {
            "purchase_order": state.review.edited_po,
            "customer_number": None,
            "lines": [],
            "issues": [],
            "decision": None,
            "explanation": None,
            "status": "running",
        }

    def reject_order(state: AgentState) -> dict:
        return {"status": "rejected"}

    def after_decide(state: AgentState) -> str:
        return "post_order" if state.decision == "auto_post" else "explain_exception"

    def after_post(state: AgentState) -> str:
        return "verify_order" if state.sales_order_number else "explain_exception"

    def after_verify(state: AgentState) -> str:
        return END if state.status == "posted" else "cancel_order"

    def after_review(state: AgentState) -> str:
        if state.issues and state.issues[-1].code == IssueCode.APPROVAL_NOT_POSSIBLE:
            return "explain_exception"
        return {
            "approve": "post_order",
            "edit": "apply_edit",
            "reject": "reject_order",
        }[state.review.action]

    graph = StateGraph(AgentState)
    for name, node in [
        ("resolve_customer", resolve_customer),
        ("resolve_materials", resolve_materials),
        ("check_prices", check_prices),
        ("check_anomalies", check_anomalies),
        ("decide", decide),
        ("post_order", post_order),
        ("verify_order", verify_order),
        ("cancel_order", cancel_order),
        ("explain_exception", explain_exception),
        ("human_review", human_review),
        ("apply_edit", apply_edit),
        ("reject_order", reject_order),
    ]:
        graph.add_node(name, node)

    graph.add_edge(START, "resolve_customer")
    graph.add_edge("resolve_customer", "resolve_materials")
    graph.add_edge("resolve_materials", "check_prices")
    graph.add_edge("check_prices", "check_anomalies")
    graph.add_edge("check_anomalies", "decide")
    graph.add_conditional_edges(
        "decide",
        after_decide,
        {"post_order": "post_order", "explain_exception": "explain_exception"},
    )
    graph.add_conditional_edges(
        "post_order",
        after_post,
        {"verify_order": "verify_order", "explain_exception": "explain_exception"},
    )
    graph.add_conditional_edges(
        "verify_order", after_verify, {END: END, "cancel_order": "cancel_order"}
    )
    graph.add_edge("cancel_order", "explain_exception")
    graph.add_edge("explain_exception", "human_review")
    graph.add_conditional_edges(
        "human_review",
        after_review,
        {
            "post_order": "post_order",
            "apply_edit": "apply_edit",
            "reject_order": "reject_order",
            "explain_exception": "explain_exception",
        },
    )
    graph.add_edge("apply_edit", "resolve_customer")
    graph.add_edge("reject_order", END)
    return graph.compile(checkpointer=checkpointer)


def run(graph, state: AgentState, config: dict | None = None) -> AgentState:
    return AgentState.model_validate(graph.invoke(state, config))
```

**`agent/checkpoint.py`** (final):

```python
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from agent.state import AgentState, Review


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        yield saver


def thread_config(idempotency_key: str) -> dict:
    return {"configurable": {"thread_id": idempotency_key}}


def is_awaiting_review(snapshot) -> bool:
    return any(task.interrupts for task in snapshot.tasks)


def run_or_resume(graph, initial: AgentState) -> AgentState:
    config = thread_config(initial.idempotency_key)
    snapshot = graph.get_state(config)
    if snapshot.next and is_awaiting_review(snapshot):
        return AgentState.model_validate(snapshot.values)
    if snapshot.next:
        result = graph.invoke(None, config)
    elif snapshot.values:
        return AgentState.model_validate(snapshot.values)
    else:
        result = graph.invoke(initial, config)
    return AgentState.model_validate(result)


def resume_with_review(graph, idempotency_key: str, review: Review) -> AgentState:
    config = thread_config(idempotency_key)
    if not is_awaiting_review(graph.get_state(config)):
        raise ValueError(f"order {idempotency_key} is not waiting for a review")
    result = graph.invoke(Command(resume=review.model_dump(mode="json")), config)
    return AgentState.model_validate(result)
```

`local/agent_review_demo.py` takes three price-deviation POs below the limit: **approve** → posted; **reject** → rejected; **edit** (price corrected to the contract price) → all checks run again → posted with no issues. A second decision on each is refused.

## Step 6 — Agent service

```bash
git checkout -b feat/agent-service
uv add --package agent fastapi uvicorn "psycopg[pool]"
```

`agent/app.py` combines a Kafka consumer thread (`orders.extracted`) and a FastAPI app (`/health`, `GET` and `POST /reviews/{idempotency_key}`). Key design points:

- The checkpointer uses a **psycopg connection pool** (`autocommit`, `prepare_threshold=0`, `dict_row`), because the consumer thread and API requests use it at the same time.
- `outcome_events` always publishes `orders.validated`, plus `orders.posted` (with `sales_order_number`) or `orders.exceptions` (with explanation, PO and `source_document`).
- Event IDs are `uuid5` of key, topic and a hash of the payload: re-publishing identical results is deduplicated downstream.
- An event without a valid `purchase_order` raises `PermanentError` (DLQ); ERP outages raise normally (retry, then resume from checkpoint).
- `POST /reviews/{key}` returns **409** when the order is not waiting. No login yet (local only; E9 adds it).

Compose service (inside `services:`):

```yaml
  agent:
    build:
      context: .
      dockerfile: docker/python-service.Dockerfile
      args:
        PACKAGE: agent
    command: uvicorn agent.app:create_app --factory --host 0.0.0.0 --port 8400
    environment:
      <<: *pipeline-env
      ERP_BASE_URL: http://erp-mock:8100
    ports:
      - "8400:8400"
    depends_on:
      kafka:
        condition: service_started
      litellm:
        condition: service_started
      erp-mock:
        condition: service_started
      postgres:
        condition: service_healthy
```

**`docker/python-service.Dockerfile`** (final, with download timeout and shared cache):

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_HTTP_TIMEOUT=120 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY prompts ./prompts

ARG PACKAGE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package ${PACKAGE}

RUN useradd --create-home --uid 10001 app
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
```

End-to-end:

```bash
for service in intake classifier extractor agent; do docker compose build $service || break; done
make up
make topics
make contracts-ingest
make send PDF=poc/data/pos/<clean PO>.pdf
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic orders.posted --from-beginning --timeout-ms 5000
```

A clean PO below the limit appears on `orders.posted` and as one `OPEN` row in `vbak` (touchless). A price-deviation PO appears on `orders.exceptions`; `GET http://localhost:8400/reviews/<key>` shows `awaiting_review: true` with the explanation; `POST` approve → posted; a second `POST` → 409.

To look up past results: the console consumer on `orders.posted` or `orders.exceptions` lists every PO number with its outcome; `GET /reviews/<key>` returns each order's explanation.

**Makefile targets added in E8:** `contracts-ingest`, `agent`.

## Troubleshooting — problems hit during E8 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | After `git commit`, files appear both staged and "not staged"; commit missing | Pre-commit hooks fixed files, which aborts the commit | `make lint` → `git add .` → commit again until it ends with `[branch …]` |
| 2 | Old scripts fail after Step 5 (`Deps` missing `explain`; interrupt error) | `Deps` gained a field; `interrupt()` needs a checkpointer | Scripts pass `explain`; dry run uses `InMemorySaver` and a `thread_id` config |
| 3 | `make up`: `Failed to download fastapi … operation timed out` | Five images downloaded the same packages in parallel; connection too slow | Dockerfile with `UV_HTTP_TIMEOUT=120` and a uv cache mount; build images one at a time |
| 4 | PowerShell: `Missing opening '(' after keyword 'for'` | Bash loop syntax typed in PowerShell | Use Git Bash, or run the four `docker compose build <service>` commands separately |
| 5 | Consumer thread and API share one database connection | A single psycopg connection is not thread-safe | `PostgresSaver` on a `psycopg_pool.ConnectionPool` |
| 6 | Agent processed PO-0001 and PO-0003 right after its first start | Their `orders.extracted` events were waiting in Kafka since E7; a new consumer group reads from the beginning | Expected behaviour of `auto.offset.reset=earliest` |

**Lessons:**

- Use a fixed graph for agents that change business data; let the LLM help, never decide.
- Check every LLM output that names master data against the real master data.
- Checkpoint plus idempotent writes make crashes harmless.
- Human review is a first-class node, not an afterthought.
