# E4 Execution Guide — Proof of Concept (Milestone M1)

How the PoC of the Order-to-Cash Agent was built and measured, in the order it was done, with the **final working version** of every file. Problems hit along the way and their fixes are in **Troubleshooting** at the end. Everything runs on the local stack from E2; no AWS is needed.

**Question the PoC answers:** can GPT reliably turn purchase-order PDFs into correct JSON, recognise POs among other documents, and check prices against customer contracts?

## Results

| Run | Perfect POs | Field accuracy | Tokens / PO | Cost / PO |
| --- | --- | --- | --- | --- |
| Baseline prompt | 3 / 50 | 96.5% | 1,363 | $0.00111 |
| Prompt v2: business context | 44 / 50 | 99.7% | 1,491 | $0.00116 |
| Prompt v3: material-number rule | 46 / 50 | 99.8% | 1,491 | $0.00111 |
| **Prompt v3 + layout-mode text** | **49 / 50** | **99.9%** | 1,513 | $0.00117 |

- Document classification: **80 / 80**
- RAG price check (on ground-truth POs): **50 / 50** — 10 deviations found, 0 false alarms
- Primary metric is **perfect POs** (touchless rate): a 96%-correct PO still needs a human.

## Step 1 — PoC environment

```powershell
git checkout main
git pull
git checkout -b feat/poc
mkdir poc/notebooks
mkdir poc/data
uv add --group poc jupyterlab openai pydantic python-dotenv pypdf reportlab faker
```

PoC packages live in a separate `poc` dependency group so they never enter production images. Makefile target (TAB before the command):

```makefile
poc:
	uv run --group poc jupyter lab
```

Start with `make up`, then `make poc`. Smoke test in `poc/notebooks/01_smoke.ipynb`:

```python
import os
from dotenv import load_dotenv, find_dotenv
from openai import OpenAI

load_dotenv(find_dotenv(usecwd=True))
llm = OpenAI(base_url="http://localhost:4000", api_key=os.environ["LITELLM_MASTER_KEY"])

reply = llm.chat.completions.create(
    model="o2c-extract",
    messages=[{"role": "user", "content": "Reply with the word READY"}],
)
print(reply.choices[0].message.content)
```

The notebook only knows the LiteLLM address and master key, never the OpenAI key.

## Step 2 — PO schema and synthetic golden set

**`poc/schema.py`**:

```python
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class LineItem(BaseModel):
    line_no: int
    material_number: str | None
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


class PurchaseOrder(BaseModel):
    po_number: str
    po_date: date
    customer_name: str
    customer_number: str | None
    currency: str
    delivery_date: date | None
    ship_to_address: str
    lines: list[LineItem]
    total_amount: Decimal
```

The generator `poc/generate_pos.py` was written by **Claude Code** (`claude` in the repo folder) from this prompt:

```
Create poc/generate_pos.py, a script that generates a synthetic dataset for the PoC.
Use the Pydantic models in poc/schema.py as the ground-truth format.
1. Create poc/data/master_data.json with 8 customers and 25 materials (Austrian/German B2B industrial supplies).
2. Generate 50 PO PDFs with reportlab into poc/data/pos/PO-0001.pdf ... PO-0050.pdf.
3. Use 6 visibly different vendor layouts; vary date and number formats.
4. 1-8 line items per PO; some lines show our material_number, some only a description.
5. Write exact ground truth to poc/data/ground_truth/PO-XXXX.json.
6. In 10 POs make one line's unit_price deviate 5-30% from list_price; record in poc/data/labels.json.
7. Fixed random seed. No network or LLM calls.
8. Add poc/tests/test_generate.py.
```

Later fix (see Troubleshooting #5): long descriptions are **wrapped, not clipped**, and a test checks that every ground-truth description and customer name appears in the PDF text.

```powershell
uv run --group poc python poc/generate_pos.py
```

Numbers in ground truth are stored as strings (`"9.80"`) and compared as exact decimals — no float rounding.

## Step 3 — Extraction notebook (02\_extract.ipynb)

**Setup**

```python
import json
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI
from pypdf import PdfReader

sys.path.insert(0, "..")
from schema import PurchaseOrder

load_dotenv(find_dotenv(usecwd=True))
llm = OpenAI(base_url="http://localhost:4000", api_key=os.environ["LITELLM_MASTER_KEY"])
DATA = Path("../data")
```

**PDF text — layout mode** (keeps table columns aligned so wrapped cells stay under their column):

```python
def pdf_text(path: Path, mode: str = "layout") -> str:
    return "\n".join(
        page.extract_text(extraction_mode=mode) or "" for page in PdfReader(path).pages
    )
```

**Prompt v3** (final):

```python
SYSTEM_PROMPT = f"""You extract purchase orders into JSON for Alpentech Industriebedarf GmbH.

Context:
- Alpentech Industriebedarf GmbH is the SUPPLIER that receives these purchase orders.
- The CUSTOMER is the company that issued the purchase order (the buyer).
  Never use Alpentech as the customer.

Field rules:
- customer_name: the buyer's legal company name only.
- ship_to_address: one line in the form "Street Number, PostalCode City, Country".
  Do not include company names, departments (e.g. "Wareneingang") or attention lines.
- material_number: our 6-digit article number (e.g. 400103), or null.
  A "-" or an empty cell means null. Never put a description in this field.
- description: copy exactly as printed. If a description wraps onto the next line,
  join the parts with a single space.
- Dates as YYYY-MM-DD.
- Numbers as plain decimals with a dot and no thousands separator, e.g. 2060.90.
- Copy text fields exactly as printed. Do not translate.
- If a field is not on the document, use null.
- line_no starts at 1, in document order.

Return only JSON matching this JSON schema: {json.dumps(PurchaseOrder.model_json_schema())}
"""
```

**Extraction with cost tracking** (LiteLLM returns cost in the `x-litellm-response-cost` header; metadata names and tags the Langfuse trace):

```python
from collections import Counter

from pydantic import ValidationError
from scoring import score


def extract_with_stats(text: str, doc_id: str):
    raw = llm.chat.completions.with_raw_response.create(
        model="o2c-extract",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        extra_body={
            "metadata": {
                "trace_name": "extract",
                "tags": ["poc", "prompt-v3-layout", doc_id],
            }
        },
    )
    response = raw.parse()
    cost = float(raw.headers.get("x-litellm-response-cost") or 0)
    try:
        result = PurchaseOrder.model_validate_json(response.choices[0].message.content)
    except ValidationError:
        result = None
    return result, response.usage.total_tokens, cost
```

## Step 4 — Field-level scoring and baseline

**`poc/scoring.py`** (reused later by the CI eval gate; tests in `poc/tests/test_scoring.py` written by Claude Code):

```python
from schema import PurchaseOrder

HEADER_FIELDS = [
    "po_number",
    "po_date",
    "customer_name",
    "customer_number",
    "currency",
    "delivery_date",
    "ship_to_address",
    "total_amount",
]
LINE_FIELDS = [
    "material_number",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "line_total",
]


def _same(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()) == " ".join(b.split())
    return a == b


def score(result: PurchaseOrder | None, truth: PurchaseOrder) -> dict:
    total = len(HEADER_FIELDS) + len(LINE_FIELDS) * len(truth.lines)
    if result is None:
        return {"correct": 0, "total": total, "errors": ["invalid output"]}
    correct, errors = 0, []
    for field in HEADER_FIELDS:
        if _same(getattr(result, field), getattr(truth, field)):
            correct += 1
        else:
            errors.append(field)
    got = {line.line_no: line for line in result.lines}
    for expected in truth.lines:
        line = got.get(expected.line_no)
        for field in LINE_FIELDS:
            if line is not None and _same(
                getattr(line, field), getattr(expected, field)
            ):
                correct += 1
            else:
                errors.append(f"line.{field}")
    extra = len(result.lines) - len(truth.lines)
    if extra > 0:
        errors.append("extra lines")
    return {"correct": correct, "total": total, "errors": errors}
```

**Run all 50 and summarise:**

```python
rows = []
for pdf in sorted((DATA / "pos").glob("*.pdf")):
    doc_id = pdf.stem
    truth = PurchaseOrder.model_validate_json(
        (DATA / "ground_truth" / f"{doc_id}.json").read_text(encoding="utf-8")
    )
    result, tokens, cost = extract_with_stats(pdf_text(pdf), doc_id)
    rows.append(
        {"doc_id": doc_id, "tokens": tokens, "cost": cost, **score(result, truth)}
    )

correct = sum(r["correct"] for r in rows)
total = sum(r["total"] for r in rows)
perfect = sum(r["correct"] == r["total"] for r in rows)
print(f"Field accuracy:   {correct / total:.1%}")
print(f"Perfect POs:      {perfect} / {len(rows)}")
print(f"Invalid outputs:  {sum('invalid output' in r['errors'] for r in rows)}")
print(f"Avg tokens / PO:  {sum(r['tokens'] for r in rows) / len(rows):.0f}")
print(f"Cost / PO:        ${sum(r['cost'] for r in rows) / len(rows):.5f}")
print(
    "Most common errors:", Counter(e for r in rows for e in r["errors"]).most_common(5)
)
```

Each run is saved to `poc/results/<run>.json` (`baseline`, `prompt_v2`, `prompt_v3`, `prompt_v3_layout`) and tagged in Langfuse for comparison.

## Step 5 — Document classification (03\_classify.ipynb)

Claude Code generated 30 non-PO PDFs (`poc/generate_other_docs.py`: invoices, remittance advices, other) into `poc/data/other/` and `poc/data/doc_types.json` with 80 labels, including deliberately confusing documents.

```python
from typing import Literal

from pydantic import BaseModel


class Classification(BaseModel):
    doc_type: Literal["purchase_order", "invoice", "remittance", "other"]
    reason: str


CLASSIFY_PROMPT = f"""You classify documents received by Alpentech Industriebedarf GmbH, a supplier.
Types:
- purchase_order: a customer orders goods FROM Alpentech.
- invoice: someone bills Alpentech or requests payment.
- remittance: a customer informs Alpentech about a payment.
- other: anything else (order confirmations, delivery notes, newsletters).
Return only JSON matching: {json.dumps(Classification.model_json_schema())}
"""
```

Only the first 4,000 characters are sent (`text[:4000]`). The `reason` field explains every decision. Result: 80/80.

## Step 6 — Contracts and pgvector index (04\_rag.ipynb)

Claude Code generated 8 framework contracts plus 2 amendments (`poc/generate_contracts.py`) as Markdown in `poc/data/contracts/`: tolerance clauses of 0%, 2% or 3% and an Annex A price table whose prices equal the master-data list prices (so `labels.json` stays valid).

Embedding model added under `model_list:` in `local/litellm/config.yaml`, then `docker compose restart litellm`:

```yaml
  - model_name: o2c-embed
    litellm_params:
      model: openai/text-embedding-3-small
      api_key: os.environ/OPENAI_API_KEY
```

```powershell
uv add --group poc langchain langchain-openai langchain-postgres langchain-text-splitters "psycopg[binary]"
```

```python
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter

embeddings = OpenAIEmbeddings(
    model="o2c-embed",
    base_url="http://localhost:4000",
    api_key=os.environ["LITELLM_MASTER_KEY"],
    check_embedding_ctx_length=False,
)
db_url = (
    f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:"
    f"{os.environ['POSTGRES_PASSWORD']}@localhost:5432/o2c"
)
store = PGVector(
    embeddings=embeddings,
    collection_name="contracts",
    connection=db_url,
    use_jsonb=True,
    pre_delete_collection=True,
)

splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
docs = []
for path in sorted((DATA / "contracts").glob("*.md")):
    customer_number = path.stem.split("-")[0]
    for chunk in splitter.split_text(path.read_text(encoding="utf-8")):
        docs.append(
            Document(
                page_content=chunk,
                metadata={"customer_number": customer_number, "source": path.name},
            )
        )
store.add_documents(docs)
```

Every chunk carries its `customer_number`; retrieval filters by it so customers' contracts never mix.

## Step 7 — RAG price check

GPT reads the retrieved contract text and returns the agreed price and tolerance; **Python does the arithmetic**. Evaluated on ground-truth POs so extraction errors cannot distort the result.

```python
class PriceTerms(BaseModel):
    agreed_unit_price: Decimal | None
    tolerance_pct: Decimal | None
    evidence: str


def check_po(po: PurchaseOrder) -> list[int]:
    customer_number = po.customer_number or customer_by_name[po.customer_name]
    flagged = []
    for line in po.lines:
        terms = get_terms(customer_number, line.material_number, line.description)
        if terms.agreed_unit_price is None:
            flagged.append(line.line_no)
            continue
        deviation = (
            abs(line.unit_price - terms.agreed_unit_price)
            / terms.agreed_unit_price
            * 100
        )
        if deviation > (terms.tolerance_pct or Decimal(0)):
            flagged.append(line.line_no)
    return flagged
```

`get_terms` retrieves 4 price chunks plus 1 tolerance chunk for the customer, asks GPT for `PriceTerms` with quoted evidence, and caches each (customer, material) pair. Result: 50/50.

## Step 8 — ADR-002 and merge

`docs/adr/0002-poc-findings.md` records results, decisions and the gap list:

- Keep gpt-4.1-mini behind alias `o2c-extract`; fine-tuning evaluated in Phase 3.
- Primary metric: perfect POs. Layout-mode text extraction. LLM extracts, Python calculates.
- Production prices from ERP tables via SQL; RAG for contract wording.
- Gaps: notebooks not services; no retries, idempotency, DLQ, crash recovery, human review, audit trail, held-out test set, scans, prompt-injection defence, PII/retention rules, or CI eval gate.

## Troubleshooting — problems hit during E4 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `APIConnectionError` / `WinError 10061 … actively refused it` in the notebook | LiteLLM not running (Docker Desktop or stack stopped) | Start Docker Desktop, run `make up`, check `make ps`, wait \~20 s, re-run the cell |
| 2 | `customer_name` wrong in 20 POs: Alpentech returned instead of the buyer | Prompt did not say which side is the customer; both companies are printed | Business context in the prompt: Alpentech is the supplier, the customer is the issuer |
| 3 | `ship_to_address` wrong in 39 POs: company name, "Wareneingang", line breaks included | Address format undefined | Rule: one line "Street Number, PostalCode City, Country", no names or departments |
| 4 | PO-0019: description put into `material_number`, "-" into `description` | "-" meant "no material number" in that layout | Rule: 6-digit number or null; "-" or empty means null |
| 5 | Material 400103 always missing "Stk" | Generator clipped long text in PDF tables; ground truth expected text not on the document | Claude Code changed the generator to wrap text; new test checks ground truth against PDF text |
| 6 | After the fix, "Stk" still dropped | Plain text extraction puts the wrapped part on its own line, where it looks like a unit | pypdf `extraction_mode="layout"`: 46 → 49 perfect POs for \~1.5% more tokens |
| 7 | PO-0050 scored 31/32, then MATCH on re-run | GPT is not fully deterministic at `temperature=0` | Evaluation gates use thresholds, not exact scores |
| 8 | `Collection not found` when indexing | Printed by `pre_delete_collection=True` on the first run | Harmless |
| 9 | Price table split across chunks | Fixed-size chunking cuts tables | Accepted for the PoC; production uses SQL for prices |
| 10 | Commit silently not made; PR merged without latest changes | A pre-commit hook failed or fixed files, which aborts the commit | Check the commit output ends with `[branch abc1234] message`; if hooks fixed files, `git add .` and commit again |
| 11 | ruff: `invalid-syntax: Unexpected indentation` in a notebook cell | Pasted cell started with spaces | Remove leading spaces in the cell, save, commit again |
| 12 | `gh pr merge`: *local changes would be overwritten by checkout* | Uncommitted files on the branch | `git stash -u` → checkout main, pull → new branch → `git stash pop` → commit → PR |
| 13 | `feat/poc` still visible after merge | `gh` aborted before deleting the remote branch | `git push origin --delete feat/poc` and `git fetch --prune` |

**Lessons:**

- Business context and input quality improved results more than prompt length.
- The golden set needs its own tests.
- Don't let the LLM do arithmetic.
- Measure the business metric (perfect POs), not only field accuracy.
