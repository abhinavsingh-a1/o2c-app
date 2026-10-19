# E2 Setup Guide — Local Development Environment (Windows)

How the complete local stack of the Order-to-Cash Agent was built on Windows, in the order it was done. Each step shows the **final, working version** of every file. Problems hit along the way and their fixes are collected in **Troubleshooting** at the end.

**Result of E2:** one command (`make up`) starts Postgres + pgvector, Kafka, SeaweedFS (S3), MLflow, LiteLLM and Langfuse; a uv Python workspace and a pnpm React workspace exist; a local kind Kubernetes cluster runs the hello service through the shared Helm chart.

| Service | URL (browser) | Purpose |
| --- | --- | --- |
| Langfuse | http://localhost:3000 | LLM traces, cost, evals |
| MLflow | http://localhost:5000 | Experiments and model registry |
| LiteLLM | http://localhost:4000 | LLM gateway in front of OpenAI |
| SeaweedFS | http://localhost:9333 (status), :8333 (S3 API) | Local S3 |
| React UI | http://localhost:5173 | Exception review UI (empty for now) |
| Postgres | localhost:5432 | Main database + pgvector |
| Kafka | localhost:9092 | Event backbone |

## Step 1 — Install WSL2 and Docker Desktop

In PowerShell **as administrator**:

```powershell
wsl --install
```

Restart the computer. Then:

```powershell
winget install Docker.DockerDesktop
```

Start Docker Desktop and wait for **Engine running**. Check in a normal PowerShell:

```powershell
docker --version
docker compose version
docker run hello-world
```

## Step 2 — Secrets file and Postgres init script

All work happens in `o2c-app` on a feature branch, merged through a PR at the end.

**`.env.example`** (committed; placeholders only):

```
POSTGRES_USER=o2c
POSTGRES_PASSWORD=change-me
S3_ACCESS_KEY=local
S3_SECRET_KEY=localsecret
OPENAI_API_KEY=sk-replace-me
LITELLM_MASTER_KEY=sk-local-master-replace-me
LANGFUSE_SALT=replace-me
LANGFUSE_ENCRYPTION_KEY=replace-me
LANGFUSE_NEXTAUTH_SECRET=replace-me
REDIS_AUTH=replace-me
CLICKHOUSE_PASSWORD=replace-me
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local-secret
LANGFUSE_ADMIN_EMAIL=replace-me
LANGFUSE_ADMIN_PASSWORD=replace-me
```

Create the real copy and confirm Git ignores it:

```powershell
Copy-Item .env.example .env
git check-ignore .env
```

Fill `.env` with real values. Passwords: **letters and numbers only** (they are placed inside URLs). Generate random 64-character values with:

```powershell
-join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) })
```

**`local/postgres-init/01-init.sql`** (runs only on the first start of an empty database):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE mlflow;
CREATE DATABASE langfuse;
CREATE DATABASE litellm;
```

## Step 3 — MLflow image and bucket script

**`local/mlflow/Dockerfile`** (adds Postgres and S3 support to the official image):

```dockerfile
FROM ghcr.io/mlflow/mlflow:v2.17.2
RUN pip install --no-cache-dir psycopg2-binary boto3
```

**`local/create_buckets.py`**:

```python
import os

import boto3
from dotenv import load_dotenv

load_dotenv()
s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:8333",
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
)
for bucket in ["mlflow", "o2c-documents", "langfuse"]:
    try:
        s3.create_bucket(Bucket=bucket)
        print(f"created {bucket}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"exists {bucket}")
```

## Step 4 — OpenAI account and LiteLLM config

1. platform.openai.com → **Billing**: add $5, **Auto recharge OFF**
2. **Limits**: monthly budget $10
3. **Projects**: create `o2c`; create an API key inside it and put it in `.env` as `OPENAI_API_KEY`

**`local/litellm/config.yaml`** (code always asks for `o2c-extract`; the real model is changed only here):

```yaml
model_list:
  - model_name: o2c-extract
    litellm_params:
      model: openai/gpt-4.1-mini
      api_key: os.environ/OPENAI_API_KEY

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL

litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
```

## Step 5 — docker-compose.yml (final version)

```yaml
x-langfuse-env: &langfuse-env
  DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/langfuse
  SALT: ${LANGFUSE_SALT}
  ENCRYPTION_KEY: ${LANGFUSE_ENCRYPTION_KEY}
  TELEMETRY_ENABLED: "false"
  CLICKHOUSE_MIGRATION_URL: clickhouse://clickhouse:9000
  CLICKHOUSE_URL: http://clickhouse:8123
  CLICKHOUSE_USER: clickhouse
  CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
  CLICKHOUSE_CLUSTER_ENABLED: "false"
  REDIS_HOST: redis
  REDIS_PORT: "6379"
  REDIS_AUTH: ${REDIS_AUTH}
  LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
  LANGFUSE_S3_EVENT_UPLOAD_REGION: us-east-1
  LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://s3:8333
  LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: ${S3_ACCESS_KEY}
  LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: ${S3_SECRET_KEY}
  LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
  LANGFUSE_S3_EVENT_UPLOAD_PREFIX: events/
  LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: langfuse
  LANGFUSE_S3_MEDIA_UPLOAD_REGION: us-east-1
  LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://localhost:8333
  LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: ${S3_ACCESS_KEY}
  LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: ${S3_SECRET_KEY}
  LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: "true"
  LANGFUSE_S3_MEDIA_UPLOAD_PREFIX: media/

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: o2c
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./local/postgres-init:/docker-entrypoint-initdb.d
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      retries: 10

  kafka:
    image: apache/kafka:3.8.0
    ports:
      - "9092:9092"
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:19092,CONTROLLER://:9093,PLAINTEXT_HOST://:9092
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:19092,PLAINTEXT_HOST://localhost:9092
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_NUM_PARTITIONS: 3

  s3:
    image: chrislusf/seaweedfs:4.48
    command: server -dir=/data -s3
    ports:
      - "8333:8333"
      - "9333:9333"
    volumes:
      - s3data:/data

  mlflow:
    build: ./local/mlflow
    command: >
      mlflow server --host 0.0.0.0 --port 5000
      --backend-store-uri postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/mlflow
      --artifacts-destination s3://mlflow
    environment:
      MLFLOW_S3_ENDPOINT_URL: http://s3:8333
      AWS_ACCESS_KEY_ID: ${S3_ACCESS_KEY}
      AWS_SECRET_ACCESS_KEY: ${S3_SECRET_KEY}
    ports:
      - "5000:5000"
    depends_on:
      postgres:
        condition: service_healthy
      s3:
        condition: service_started

  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    volumes:
      - ./local/litellm/config.yaml:/app/config.yaml
    environment:
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/litellm
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
      LANGFUSE_HOST: http://langfuse-web:3000
      LANGFUSE_BASE_URL: http://langfuse-web:3000
      LANGFUSE_OTEL_HOST: http://langfuse-web:3000
    ports:
      - "4000:4000"
    depends_on:
      postgres:
        condition: service_healthy

  clickhouse:
    image: clickhouse/clickhouse-server:24.8
    environment:
      CLICKHOUSE_DB: default
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD}
    volumes:
      - chdata:/var/lib/clickhouse
    ulimits:
      nofile:
        soft: 262144
        hard: 262144

  redis:
    image: redis:7
    command: redis-server --requirepass ${REDIS_AUTH} --maxmemory-policy noeviction

  langfuse-worker:
    image: langfuse/langfuse-worker:3
    environment: *langfuse-env
    depends_on:
      postgres:
        condition: service_healthy
      clickhouse:
        condition: service_started
      redis:
        condition: service_started

  langfuse-web:
    image: langfuse/langfuse:3
    ports:
      - "3000:3000"
    environment:
      <<: *langfuse-env
      NEXTAUTH_URL: http://localhost:3000
      NEXTAUTH_SECRET: ${LANGFUSE_NEXTAUTH_SECRET}
      LANGFUSE_INIT_ORG_ID: o2c
      LANGFUSE_INIT_ORG_NAME: O2C
      LANGFUSE_INIT_PROJECT_ID: o2c-local
      LANGFUSE_INIT_PROJECT_NAME: o2c-local
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
      LANGFUSE_INIT_USER_EMAIL: ${LANGFUSE_ADMIN_EMAIL}
      LANGFUSE_INIT_USER_NAME: admin
      LANGFUSE_INIT_USER_PASSWORD: ${LANGFUSE_ADMIN_PASSWORD}
    depends_on:
      postgres:
        condition: service_healthy
      clickhouse:
        condition: service_started
      redis:
        condition: service_started

volumes:
  pgdata:
  s3data:
  chdata:
```

Key points:

- Kafka has two addresses: `localhost:9092` for programs on the PC, `kafka:19092` for containers.
- `langfuse-web:3000` works only between containers; the browser uses `localhost:3000`.
- The `LANGFUSE_INIT_` variables create the user, project and API keys on first start.
- SeaweedFS is pinned to `4.48` (found with `docker compose exec s3 weed version`).

## Step 6 — Start and verify the stack

```powershell
docker compose up -d --build
docker compose ps
uv run --with boto3 --with python-dotenv python local/create_buckets.py
```

| Check | Command / action | Expected |
| --- | --- | --- |
| pgvector | `docker compose exec postgres psql -U o2c -d o2c -c "\dx"` | `vector` listed |
| Kafka | `docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list` | topic list returns |
| MLflow | open http://localhost:5000, log a test run | run with `hello.txt` artifact |
| LiteLLM | test command below | short greeting |
| Langfuse | http://localhost:3000 → o2c-local → Tracing | trace with prompt, answer, cost |

MLflow test run:

```powershell
uv run --with mlflow==2.17.2 python -c "import mlflow; mlflow.set_tracking_uri('http://localhost:5000'); mlflow.set_experiment('smoke'); r = mlflow.start_run(); mlflow.log_metric('ok', 1); mlflow.log_text('hello', 'hello.txt'); mlflow.end_run(); print('logged')"
```

LiteLLM test call (also produces a Langfuse trace):

```powershell
uv run --with openai --with python-dotenv python -c "import os; from dotenv import load_dotenv; load_dotenv(); from openai import OpenAI; c = OpenAI(base_url='http://localhost:4000', api_key=os.environ['LITELLM_MASTER_KEY']); print(c.chat.completions.create(model='o2c-extract', messages=[{'role':'user','content':'Say hello'}]).choices[0].message.content)"
```

## Step 7 — Makefile

Install make, then **close all terminals** and reopen:

```powershell
winget install ezwinports.make
make --version
```

**`Makefile`** (indented lines must start with a **TAB**, not spaces):

```makefile
.PHONY: up down ps logs buckets lint clean sync test ui cluster-up cluster-down

up:
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail 50

buckets:
	uv run --with boto3 --with python-dotenv python local/create_buckets.py

lint:
	pre-commit run --all-files

clean:
	docker compose down -v

sync:
	uv sync --all-packages

test:
	uv run pytest

ui:
	pnpm --filter ui dev

cluster-up:
	kind create cluster --config local/kind-config.yaml

cluster-down:
	kind delete cluster --name o2c
```

`make clean` deletes all local data (databases, buckets, traces).

**Added later in E4:** a `poc` target (`uv run --group poc jupyter lab`) in the Makefile, and an `o2c-embed` embedding model (`openai/text-embedding-3-small`) in `local/litellm/config.yaml`. See the E4 guide.

## Step 8 — Python workspace (uv)

```powershell
uv python install 3.12
[Environment]::SetEnvironmentVariable("UV_LINK_MODE", "copy", "User")
```

**`pyproject.toml`** (repo root):

```toml
[project]
name = "o2c"
version = "0.1.0"
requires-python = ">=3.12"

[tool.uv.workspace]
members = ["libs/*", "services/*"]

[dependency-groups]
dev = ["pytest>=8", "httpx"]

[tool.pytest.ini_options]
testpaths = ["libs", "services"]
addopts = "--import-mode=importlib"
```

Shared library:

```powershell
mkdir services
uv init --lib libs/o2c-common
```

**`libs/o2c-common/src/o2c_common/ids.py`**:

```python
import hashlib


def idempotency_key(source: str, content: bytes) -> str:
    """Same document from the same source always gives the same key."""
    return hashlib.sha256(source.encode() + b":" + content).hexdigest()
```

**`libs/o2c-common/tests/test_ids.py`**:

```python
from o2c_common.ids import idempotency_key


def test_same_input_same_key():
    assert idempotency_key("email", b"po") == idempotency_key("email", b"po")


def test_different_content_different_key():
    assert idempotency_key("email", b"po1") != idempotency_key("email", b"po2")
```

The package folder also needs `libs/o2c-common/src/o2c_common/__init__.py` (may be empty). Then:

```powershell
make sync
make test
```

## Step 9 — TypeScript workspace (pnpm + React)

```powershell
winget install OpenJS.NodeJS.LTS
```

Reopen the terminal, then:

```powershell
npm install -g pnpm
```

**`pnpm-workspace.yaml`**:

```yaml
packages:
  - "ui"
```

**`package.json`**:

```json
{
  "name": "o2c",
  "private": true
}
```

```powershell
pnpm create vite ui --template react-ts
pnpm install
make ui
```

Answer **No** to "Install with pnpm and start now?". Add `node_modules/` and `dist/` to `.gitignore`. ESLint hook added to `.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: eslint
        name: eslint
        entry: pnpm --dir ui lint
        language: system
        files: ^ui/.*\.(ts|tsx)$
        pass_filenames: false
```

## Step 10 — kind cluster

```powershell
winget install Kubernetes.kind
winget install Kubernetes.kubectl
winget install Helm.Helm
```

**`local/kind-config.yaml`**:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: o2c
nodes:
  - role: control-plane
```

```powershell
make cluster-up
kubectl get nodes
```

Run `make cluster-down` when not in use to free memory.

## Step 11 — Hello service and Docker image

```powershell
uv init --package services/hello
uv add --package hello fastapi uvicorn
uv add --dev httpx
```

**`services/hello/src/hello/main.py`**:

```python
from fastapi import FastAPI

app = FastAPI(title="hello")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

**`services/hello/tests/test_main.py`**:

```python
from fastapi.testclient import TestClient

from hello.main import app


def test_health():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

**`.dockerignore`**:

```
.env
.git
.venv
**/node_modules
ui
local
```

**`services/hello/Dockerfile`**:

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN uv sync --frozen --no-dev --package hello

RUN useradd --create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "hello.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```powershell
make test
docker build -f services/hello/Dockerfile -t o2c-hello:0.1.0 .
docker run --rm -p 8000:8000 o2c-hello:0.1.0
kind load docker-image o2c-hello:0.1.0 --name o2c
```

## Step 12 — Shared Helm chart (o2c-infra)

**`helm/o2c-service/Chart.yaml`**:

```yaml
apiVersion: v2
name: o2c-service
description: Shared chart for all O2C services
type: application
version: 0.1.0
appVersion: "0.1.0"
```

**`helm/o2c-service/values.yaml`**:

```yaml
image:
  repository: o2c-hello
  tag: "0.1.0"
  pullPolicy: IfNotPresent
replicaCount: 1
containerPort: 8000
healthPath: /health
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 250m
    memory: 256Mi
env: {}
```

**`helm/o2c-service/templates/deployment.yaml`**:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
  labels:
    app: {{ .Release.Name }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: app
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ .Values.containerPort }}
          readinessProbe:
            httpGet:
              path: {{ .Values.healthPath }}
              port: {{ .Values.containerPort }}
          livenessProbe:
            httpGet:
              path: {{ .Values.healthPath }}
              port: {{ .Values.containerPort }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          {{- with .Values.env }}
          env:
            {{- range $key, $value := . }}
            - name: {{ $key }}
              value: {{ $value | quote }}
            {{- end }}
          {{- end }}
```

**`helm/o2c-service/templates/service.yaml`**:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ .Release.Name }}
spec:
  selector:
    app: {{ .Release.Name }}
  ports:
    - port: 80
      targetPort: {{ .Values.containerPort }}
```

Install and test:

```powershell
helm lint helm/o2c-service
helm install hello helm/o2c-service --namespace o2c --create-namespace
kubectl get pods -n o2c
kubectl port-forward -n o2c svc/hello 18080:80
```

Open http://localhost:18080/health → `{"status":"ok"}`.

## Troubleshooting — problems hit during E2 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | `pull access denied for minio/minio, repository does not exist` | MinIO stopped publishing free images; Docker Hub repo removed in Sep 2026 | Replaced MinIO with **SeaweedFS** (`chrislusf/seaweedfs`), S3 API on port 8333 |
| 2 | `The "S3_ACCESS_KEY" variable is not set` | New variables added only to `.env.example`, not to `.env` | Add every variable to **both** files; `.env` holds real values |
| 3 | `service "s3" refers to undefined volume s3data` | `volumes:` at the bottom still listed `miniodata` | Every named volume used by a service must be listed under top-level `volumes:` |
| 4 | No traces in Langfuse; LiteLLM log: *Langfuse rejected an export to https://cloud.langfuse.com … HTTP 401* | LiteLLM sent traces to Langfuse Cloud: newer Langfuse libraries read `LANGFUSE_BASE_URL`, not only `LANGFUSE_HOST` | Set `LANGFUSE_HOST`, `LANGFUSE_BASE_URL` and `LANGFUSE_OTEL_HOST` to `http://langfuse-web:3000` |
| 5 | Environment change had no effect after `docker compose restart` | `restart` keeps the old container and its old environment | Use `docker compose up -d <service>` to recreate it; verify with `docker compose exec litellm env` |
| 6 | http://langfuse-web:3000 does not open in the browser | That hostname exists only inside the Docker network | Browser uses http://localhost:3000; containers use `langfuse-web:3000` |
| 7 | `make : The term 'make' is not recognized` | First install did not complete / PATH not reloaded | Re-run `winget install ezwinports.make`, then close **all** terminals (including VS Code) and reopen |
| 8 | `ModuleNotFoundError: No module named 'o2c_common'` | `uv sync` installs only the root project, not workspace members | Use `uv sync --all-packages` (`make sync`) |
| 9 | `Expected a Python module at: libs\o2c-common\src\o2c_common\__init__.py` | `__init__.py` missing in the package folder | `New-Item -ItemType File -Force libs/o2c-common/src/o2c_common/__init__.py` |
| 10 | `warning: Failed to hardlink files; falling back to full copy` | uv cache on drive C:, project on drive G: | Harmless; hide with `UV_LINK_MODE=copy` user variable |
| 11 | `Unable to listen on port 8080 … forbidden by its access permissions` | Windows/WSL reserves port ranges including 8080 | Use another local port: `kubectl port-forward -n o2c svc/hello 18080:80`; list reserved ranges with `netsh interface ipv4 show excludedportrange protocol=tcp` |

**Preventive rules learned:**

- Pin image versions; `latest` and deleted repos break a stack without warning.
- After editing `.env` or `docker-compose.yml`, run `docker compose up -d`, not `restart`.
- Postgres passwords: letters and numbers only (they go inside URLs).
- Makefile lines start with a TAB.
- After installing a tool with winget, open a new terminal.
