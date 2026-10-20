# E7 Execution Guide — Intake, Classification and Extraction Services

How the PoC notebooks became three production services connected by Kafka, in the order it was done, with the **final working version** of every file. Problems and fixes are in **Troubleshooting** at the end. All commands run in Git Bash from the `o2c-app` folder.

**Result of E7:** `make up` runs the whole pipeline in Docker Compose. `make send PDF=<file>` → **intake** (Flask) → `docs.received` → **classifier** → `docs.classified` → **extractor** (FastAPI + LangChain) → `orders.extracted`. Duplicates are recognised end to end; non-POs are classified and skipped; every PO's LLM calls are grouped in one Langfuse session.

| Service | Framework | Port | Consumes | Produces |
| --- | --- | --- | --- | --- |
| intake | Flask + gunicorn | 8200 | HTTP POST | docs.received |
| classifier | plain Python | 8301 (health) | docs.received | docs.classified |
| extractor | FastAPI | 8300 | docs.classified | orders.extracted |

## Step 1 — Intake webhook (Flask)

```bash
git checkout main
git pull
git checkout -b feat/intake-service
uv init --package services/intake
uv add --package intake flask boto3 pydantic-settings o2c-common gunicorn
python -c "import secrets; print(secrets.token_hex(32))"
```

Add `INTAKE_HMAC_SECRET=<random value>` to `.env` and `INTAKE_HMAC_SECRET=replace-me` to `.env.example`.

Why Flask: intake is a tiny synchronous "check → store → publish" endpoint. Why HMAC: sender and intake share a secret; a matching signature proves the sender knows it and the body was not changed.

**`services/intake/src/intake/app.py`**:

```python
import base64
import binascii
import hashlib
import hmac
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import boto3
from flask import Flask, jsonify, request
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from o2c_common.events import Envelope
from o2c_common.ids import idempotency_key
from o2c_common.kafka import EventProducer

MAX_PDF_BYTES = 10 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    intake_hmac_secret: str
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "o2c-documents"


class IntakeRequest(BaseModel):
    source: Literal["email", "upload"]
    filename: str
    sender: str | None = None
    content_base64: str


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(settings: Settings | None = None, s3=None, producer=None) -> Flask:
    settings = settings or Settings()
    s3 = s3 or boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    producer = producer or EventProducer(settings.kafka_bootstrap, "intake")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/intake")
    def intake():
        body = request.get_data()
        signature = request.headers.get("X-O2C-Signature", "")
        if not hmac.compare_digest(sign(settings.intake_hmac_secret, body), signature):
            return jsonify(error="invalid signature"), 401
        try:
            req = IntakeRequest.model_validate_json(body)
        except ValidationError as exc:
            return jsonify(error="invalid request", details=json.loads(exc.json())), 400
        if not req.filename.lower().endswith(".pdf"):
            return jsonify(error="only PDF files are accepted"), 415
        try:
            content = base64.b64decode(req.content_base64, validate=True)
        except binascii.Error:
            return jsonify(error="content_base64 is not valid base64"), 400
        if len(content) > MAX_PDF_BYTES:
            return jsonify(error="file larger than 10 MB"), 413
        if not content.startswith(b"%PDF-"):
            return jsonify(error="file is not a PDF"), 415

        key = idempotency_key(req.source, content)
        s3_key = f"incoming/{key}.pdf"
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf",
        )
        event = Envelope(
            event_id=uuid5(NAMESPACE_URL, f"intake/{key}"),
            event_type="docs.received",
            producer="intake",
            idempotency_key=key,
            correlation_id=key,
            payload={
                "s3_bucket": settings.s3_bucket,
                "s3_key": s3_key,
                "filename": req.filename,
                "source": req.source,
                "sender": req.sender,
                "size_bytes": len(content),
            },
        )
        producer.send("docs.received", event)
        producer.flush()
        return jsonify(idempotency_key=key, event_id=str(event.event_id)), 202

    return app
```

Check order: signature → format → size → work. `hmac.compare_digest` compares in constant time. The file must start with `%PDF-`. Same document → same key and same `event_id`. `202 Accepted` because processing continues in the background.

**`local/send_po.py`** (does what Zapier will do):

```python
import base64
import json
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

from intake.app import sign

secret = dotenv_values(".env")["INTAKE_HMAC_SECRET"]
pdf = Path(sys.argv[1])
body = json.dumps(
    {
        "source": "upload",
        "filename": pdf.name,
        "sender": "test@example.com",
        "content_base64": base64.b64encode(pdf.read_bytes()).decode(),
    }
).encode()

response = httpx.post(
    "http://localhost:8200/intake",
    content=body,
    headers={"Content-Type": "application/json", "X-O2C-Signature": sign(secret, body)},
)
print(response.status_code, response.json())
```

Verified: the same PDF sent twice gives two `docs.received` messages with the **identical** `event_id`; a wrong signature returns `{"error":"invalid signature"}`. Tests (`services/intake/tests/test_app.py`, by Claude Code) use FakeS3, FakeProducer and Flask's test client.

## Step 2 — Classifier service

```bash
git checkout -b feat/classifier-service
uv init --package services/classifier
uv add --package classifier boto3 openai pydantic-settings o2c-common
uv add --package o2c-common pypdf
mkdir prompts
```

`.env`: `EVENTS_DATABASE_URL=postgresql+psycopg://o2c:<password>@localhost:5432/o2c` (placeholder in `.env.example`).

**`libs/o2c-common/src/o2c_common/pdf.py`**:

```python
from io import BytesIO

from pypdf import PdfReader


def pdf_text(data: bytes, mode: str = "layout") -> str:
    return "\n".join(
        page.extract_text(extraction_mode=mode) or ""
        for page in PdfReader(BytesIO(data)).pages
    )
```

**`prompts/classify.md`**:

```markdown
You classify documents received by Alpentech Industriebedarf GmbH, a supplier.
Types:
- purchase_order: a customer orders goods FROM Alpentech.
- invoice: someone bills Alpentech or requests payment.
- remittance: a customer informs Alpentech about a payment.
- other: anything else (order confirmations, delivery notes, newsletters).
Return only JSON matching: {schema}
```

**`services/classifier/src/classifier/app.py`**:

```python
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal

import boto3
from botocore.exceptions import ClientError
from openai import OpenAI
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PermanentError, PostgresStore
from o2c_common.pdf import pdf_text

REPO_ROOT = Path(__file__).resolve().parents[4]


class Classification(BaseModel):
    doc_type: Literal["purchase_order", "invoice", "remittance", "other"]
    reason: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str
    events_database_url: str
    prompts_dir: Path = REPO_ROOT / "prompts"
    health_port: int = 8301


def load_prompt(prompts_dir: Path) -> str:
    template = (prompts_dir / "classify.md").read_text(encoding="utf-8")
    return template.replace("{schema}", json.dumps(Classification.model_json_schema()))


def classify(
    llm: OpenAI, system_prompt: str, text: str, event: Envelope
) -> Classification:
    response = llm.chat.completions.create(
        model="o2c-extract",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:4000]},
        ],
        extra_body={
            "metadata": {
                "trace_name": "classify",
                "session_id": event.correlation_id,
                "tags": ["classifier"],
            }
        },
    )
    return Classification.model_validate_json(response.choices[0].message.content)


def make_handler(s3, llm: OpenAI, producer: EventProducer, system_prompt: str):
    def handle(event: Envelope) -> None:
        p = event.payload
        try:
            pdf = s3.get_object(Bucket=p["s3_bucket"], Key=p["s3_key"])["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise PermanentError(f"document missing: {p['s3_key']}") from exc
            raise
        result = classify(llm, system_prompt, pdf_text(pdf), event)
        producer.send(
            "docs.classified",
            event.child(
                "docs.classified",
                "classifier",
                {**p, "doc_type": result.doc_type, "reason": result.reason},
            ),
        )

    return handle


def start_health_server(port: int) -> None:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/health"
            self.send_response(200 if ok else 404)
            self.end_headers()
            if ok:
                self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings()
    start_health_server(settings.health_port)
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    llm = OpenAI(
        base_url=settings.litellm_base_url, api_key=settings.litellm_master_key
    )
    producer = EventProducer(settings.kafka_bootstrap, "classifier")
    EventConsumer(
        bootstrap=settings.kafka_bootstrap,
        group_id="classifier",
        topics=["docs.received"],
        handler=make_handler(s3, llm, producer, load_prompt(settings.prompts_dir)),
        producer=producer,
        store=PostgresStore(settings.events_database_url),
    ).run()


if __name__ == "__main__":
    main()
```

Every document gets a `docs.classified` event (audit record even for skipped documents). Missing PDF → `PermanentError` (DLQ); other S3 errors are retried. `session_id` groups all LLM calls of one PO in Langfuse.

Verified: exactly one `docs.classified` message for the two duplicate intake events, `doc_type: purchase_order`, `causation_id` = intake event ID; one row in `processed_events`.

## Step 3 — Extractor service (FastAPI + LangChain)

```bash
git checkout -b feat/extractor-service
uv init --package services/extractor
uv add --package extractor fastapi uvicorn python-multipart boto3 langchain-openai pydantic-settings o2c-common
cp poc/schema.py libs/o2c-common/src/o2c_common/schema.py
```

**`prompts/extract.md`** is prompt v3 from the PoC with the last line `Return only JSON matching this JSON schema: {schema}`.

**`services/extractor/src/extractor/app.py`**:

```python
import json
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, File, HTTPException, UploadFile
from langchain_openai import ChatOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PermanentError, PostgresStore
from o2c_common.pdf import pdf_text
from o2c_common.schema import PurchaseOrder

REPO_ROOT = Path(__file__).resolve().parents[4]
log = logging.getLogger("extractor")

Extract = Callable[[str, str], PurchaseOrder]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str
    events_database_url: str
    prompts_dir: Path = REPO_ROOT / "prompts"


def load_prompt(prompts_dir: Path) -> str:
    template = (prompts_dir / "extract.md").read_text(encoding="utf-8")
    return template.replace("{schema}", json.dumps(PurchaseOrder.model_json_schema()))


def make_extractor(settings: Settings) -> Extract:
    system_prompt = load_prompt(settings.prompts_dir)

    def extract(text: str, session_id: str) -> PurchaseOrder:
        llm = ChatOpenAI(
            model="o2c-extract",
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_master_key,
            temperature=0,
            extra_body={
                "metadata": {
                    "trace_name": "extract",
                    "session_id": session_id,
                    "tags": ["extractor"],
                }
            },
        )
        chain = llm.with_structured_output(PurchaseOrder, method="json_mode")
        return chain.invoke([("system", system_prompt), ("human", text)])

    return extract


def make_handler(s3, extract: Extract, producer: EventProducer):
    def handle(event: Envelope) -> None:
        p = event.payload
        if p.get("doc_type") != "purchase_order":
            log.info("skipping %s: doc_type=%s", event.event_id, p.get("doc_type"))
            return
        try:
            pdf = s3.get_object(Bucket=p["s3_bucket"], Key=p["s3_key"])["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise PermanentError(f"document missing: {p['s3_key']}") from exc
            raise
        po = extract(pdf_text(pdf), event.correlation_id)
        payload = {k: v for k, v in p.items() if k != "reason"}
        payload["purchase_order"] = po.model_dump(mode="json")
        producer.send(
            "orders.extracted", event.child("orders.extracted", "extractor", payload)
        )

    return handle


def create_app(
    settings: Settings | None = None,
    start_consumer: bool = True,
    extract: Extract | None = None,
) -> FastAPI:
    settings = settings or Settings()
    extract = extract or make_extractor(settings)
    state: dict[str, threading.Thread | None] = {"consumer": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_consumer:
            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
            )
            s3 = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
            )
            producer = EventProducer(settings.kafka_bootstrap, "extractor")
            consumer = EventConsumer(
                bootstrap=settings.kafka_bootstrap,
                group_id="extractor",
                topics=["docs.classified"],
                handler=make_handler(s3, extract, producer),
                producer=producer,
                store=PostgresStore(settings.events_database_url),
            )
            thread = threading.Thread(target=consumer.run, name="consumer", daemon=True)
            thread.start()
            state["consumer"] = thread
        yield

    app = FastAPI(title="Extractor", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        thread = state["consumer"]
        if start_consumer and (thread is None or not thread.is_alive()):
            raise HTTPException(503, "consumer not running")
        return {"status": "ok"}

    @app.post("/extract", response_model=PurchaseOrder)
    def extract_upload(file: Annotated[UploadFile, File()]) -> PurchaseOrder:
        data = file.file.read()
        if not data.startswith(b"%PDF-"):
            raise HTTPException(415, "file is not a PDF")
        return extract(pdf_text(data), f"manual-{file.filename}")

    return app
```

The Kafka consumer runs in a background thread; `/health` returns 503 if it dies. `POST /extract` extracts an uploaded PDF directly (debugging, later evals). Run locally: `uv run uvicorn extractor.app:create_app --factory --port 8300`.

Verified: `orders.extracted` message for PO-0001 matching the ground truth, `causation_id` = classifier event ID; Langfuse session holds `classify` and `extract`; `/extract` with PO-0002 returns the correct JSON.

## Step 4 — Whole pipeline in Docker Compose

**`docker/python-service.Dockerfile`** (one Dockerfile for all Python services):

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY prompts ./prompts

ARG PACKAGE
RUN uv sync --frozen --no-dev --package ${PACKAGE}

RUN useradd --create-home --uid 10001 app
USER 10001
ENV PATH="/app/.venv/bin:$PATH"
```

**Additions to `docker-compose.yml`** — shared settings at the top (below `x-langfuse-env`, above `services:`):

```yaml
x-pipeline-env: &pipeline-env
  KAFKA_BOOTSTRAP: kafka:19092
  S3_ENDPOINT_URL: http://s3:8333
  S3_ACCESS_KEY: ${S3_ACCESS_KEY}
  S3_SECRET_KEY: ${S3_SECRET_KEY}
  LITELLM_BASE_URL: http://litellm:4000
  LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
  EVENTS_DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/o2c
  INTAKE_HMAC_SECRET: ${INTAKE_HMAC_SECRET}
```

Services (inside `services:`, before `volumes:`):

```yaml
  intake:
    build:
      context: .
      dockerfile: docker/python-service.Dockerfile
      args:
        PACKAGE: intake
    command: gunicorn --bind 0.0.0.0:8200 --workers 2 "intake.app:create_app()"
    environment: *pipeline-env
    ports:
      - "8200:8200"
    depends_on:
      kafka:
        condition: service_started
      s3:
        condition: service_started

  classifier:
    build:
      context: .
      dockerfile: docker/python-service.Dockerfile
      args:
        PACKAGE: classifier
    command: python -m classifier.app
    environment: *pipeline-env
    depends_on:
      kafka:
        condition: service_started
      s3:
        condition: service_started
      litellm:
        condition: service_started
      postgres:
        condition: service_healthy

  extractor:
    build:
      context: .
      dockerfile: docker/python-service.Dockerfile
      args:
        PACKAGE: extractor
    command: uvicorn extractor.app:create_app --factory --host 0.0.0.0 --port 8300
    environment: *pipeline-env
    ports:
      - "8300:8300"
    depends_on:
      kafka:
        condition: service_started
      s3:
        condition: service_started
      litellm:
        condition: service_started
      postgres:
        condition: service_healthy
```

End-to-end test:

```bash
make up
make topics
make send PDF=poc/data/pos/PO-0003.pdf
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic orders.extracted --from-beginning --timeout-ms 5000
```

Expected: a PO-0003 `orders.extracted` message without starting anything by hand; a non-PO produces no `orders.extracted`; sending PO-0003 again produces no second message.

**Makefile targets added in E7:** `intake`, `classifier`, `extractor`, `send`.

## Troubleshooting — problems hit during E7 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `send_po.py`: *WinError 10061 … actively refused it* | Intake was not running; `make intake` did not exist yet at that point | Start intake directly with `uv run flask --app "intake.app:create_app" run --port 8200` |
| 2 | `Failed to acquire idempotence PID … Coordinator load in progress: retrying` | Kafka was still starting after a restart | Harmless; the producer retries. Wait \~30 s |
| 3 | PowerShell: `Invoke-WebRequest : Cannot bind parameter 'Headers'` | In PowerShell, `curl` is an alias for `Invoke-WebRequest` | Use `curl.exe`, or Git Bash |
| 4 | `feat/intake-service` still existed when Step 2 started | Step 1 ended with a commit but no PR | Merge every step's branch through a PR before branching the next one |
| 5 | Instructions not explicit enough (e.g. "create file?") | Steps assumed context | Every step now states: new file vs existing file, full path, exact place to edit, expected result |
| 6 | Git Bash: `exec: "C:/Program Files/Git/opt/kafka/bin/…": no such file` | Git Bash converts Linux paths like `/opt/…` into Windows paths | `echo 'export MSYS_NO_PATHCONV=1' >> ~/.bashrc` and `source ~/.bashrc` |
| 7 | `no such service: intake` | New services not saved or not placed inside `services:` with 2-space indentation | Save; check with `docker compose config --services`; services before `volumes:` |
| 8 | Local runs and containers fight over ports and consumer groups | Same group ids and ports | Stop all manually started services before `make up` |

**Lessons:**

- Pick each framework for a reason: Flask for a tiny sync webhook, FastAPI for an API with docs and a background consumer.
- Deterministic event IDs make duplicate detection work across services.
- Prompts are files in Git, reviewed like code.
- One parameterised Dockerfile keeps all Python services consistent.
