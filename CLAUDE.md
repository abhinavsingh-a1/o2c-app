# Order-to-Cash Agent

Turns emailed purchase orders (PDF) into ERP sales orders.
Flow: intake → classify → extract → validate (LangGraph agent) → post order → update CRM.
Exceptions go to a human review UI and to ServiceNow.

## Repos
- o2c-app: all services, ML pipelines, dbt, prompts, evals (this repo)
- o2c-infra: Terraform, Ansible, Helm chart
- o2c-gitops: Helm values per environment (dev/test/prod)

## Stack
Python (FastAPI, Flask, LangGraph, LangChain), TypeScript (React),
PostgreSQL + pgvector, Kafka, LiteLLM, Langfuse, MLflow.

## Rules
- Never commit secrets. This repo is public. Use .env (git-ignored).
- All LLM calls go through LiteLLM, never directly to OpenAI.
- Tests use recorded LLM responses; no live LLM calls in tests.
- Every Kafka consumer must be idempotent (use the idempotency key).
- Every Kafka consumer uses o2c_common.kafka.EventConsumer; every event is an o2c_common.events.Envelope.
- Outgoing events are created with envelope.child(...), never Envelope.new(...), except at intake.
- Commit messages and PR titles follow Conventional Commits.
- Work on a branch and open a PR; never push to main.
- Write tests for every new function.
- Record important decisions as ADRs in docs/adr/.
- The agent never lets the LLM decide whether an order is posted; code decides from explicit rules.
- LLM outputs that reference master data (e.g. material numbers) must be checked against real ERP data before use.
- The agent always runs through agent.checkpoint.run_or_resume with thread_id = idempotency key; never invoke the graph without a checkpointer in services.
- Every /reviews endpoint requires a logged-in user (CurrentUser); the reviewer is always taken from the token.
- review_decisions is append-only: never update or delete audit rows.
- Add Python packages only with `uv add` (never pip install), so uv.lock stays the single source of truth.
- Never tune prompts or models on ml/data/holdout; it is only for final measurement.
- Models are promoted only through MLflow aliases (@champion) by the promotion rule; restart the agent to load a new champion.

## Commands
- make up: start local stack (Postgres, Kafka, SeaweedFS, MLflow, LiteLLM, Langfuse)
- make down: stop it
- make ps / make logs: status and logs
- make buckets: create S3 buckets
- make lint: run all pre-commit checks
- make clean: stop and DELETE all local data
- make sync: install all Python workspace packages
- make test: run Python tests
- make ui: start the React UI at http://localhost:5173
- make erp-migrate / make erp-seed / make erp-seed-history: ERP database setup
- make erp-api: run the ERP mock API locally at http://localhost:8100/docs
- make topics / make topics-reset: create Kafka topics / delete and recreate them (localhost only)
- make dlq-list / make dlq-replay: inspect / replay the dead-letter queue
- make lag: consumer lag of all groups
- make intake: run the intake webhook at http://localhost:8200
- make classifier: run the classifier consumer
- make extractor: run the extractor at http://localhost:8300/docs
- Prompts live in prompts/*.md. Structured LLM calls use strict structured outputs (schema sent to the API) or, for Decimal/date models, a field list with a JSON example in the prompt; never paste model_json_schema() into a prompt (ADR-008).
- make send PDF=<path>: send a PDF to intake (the full pipeline runs in Docker via make up)
- make agent: run the agent locally at http://localhost:8400/docs (stop the Compose agent first)
- Review a paused order: GET/POST http://localhost:8400/reviews/{idempotency_key}
- make gen-api: regenerate ui/src/api/schema.d.ts from the agent's OpenAPI (agent must be running)
- Review API: GET /reviews?status=awaiting_review|all|posted, GET /reviews/{key}, GET /reviews/{key}/document, POST /reviews/{key}
- uv run python -m agent.auth add-user <username> "<Full Name>": create a reviewer in local/users.json (git-ignored)
- make ui-test: run the UI tests (Vitest)
- The full system including the UI runs with make up; UI at http://localhost:5180
- make ml-image / make ml-pipeline-local / make ml-pipeline-compile: build and run the anomaly training pipeline
- uv run python -m erp_mock.seed_history --reset: regenerate ERP history after customer profiles change
- make eval-quick / make eval: evaluation gate against eval/thresholds.yaml (report in eval/report.md, exit 1 on FAIL)
- uv run python ml/litellm_keys.py / ml/litellm_spend.py: create the per-service LiteLLM keys / show spend per key
- make grafana / make prom-alerts / make prom-test: dashboard URLs / firing alerts / promtool config check and alert unit tests
- Metrics: define them only in o2c_common.metrics; labels only from ALLOWED_LABELS (never idempotency keys, ids or raw paths, ADR-012)
- uv run python ml/check_env.py: report .env lines the tools read differently (never prints values); quote special values with single quotes
- uv run python ml/verify_audit.py: verify the hash-chained audit archive in s3://o2c-audit (exit 1 at the first broken record, ADR-014)
- make dbt-build / make dbt-docs: build and test the analytics marts (analytics/dbt, connection from .env) / dbt docs at http://localhost:8580
- make models-publish: push the @champion models to ghcr.io as OCI artifacts (oras, gh auth token with write:packages) and pin them in models.lock.json; images bake the pinned model, services fall back to MLflow only without one (ADR-016)
- scripts/verify_image.sh <image@sha256:digest>: cosign signature + provenance and SBOM attestations of release.yml on main (PASS/FAIL); deploy only by digest
- Environments (ADR-017): O2C_ENV=dev|test|prod prefixes Kafka topics and consumer groups (dev.docs.received, group dev.classifier) inside o2c_common only; code always uses logical names. make topics ENV=dev / make dlq-list ENV=dev
- Local platform (ADR-017): kind cluster o2c, namespaces o2c-dev/test/prod, Argo CD + Kyverno from ../o2c-infra/platform/kind (bootstrap.sh, secrets.sh <ns>, argocd-ui.sh, teardown.sh); infrastructure stays in Compose, reached via host.docker.internal (Kafka :29092)
- Analytics: analytics-sink writes raw.*; dbt builds analytics.*; Grafana reads analytics.* only as grafana_ro (ADR-013). Event payload JSON is read only in the dbt staging models.

Local URLs: MLflow http://localhost:5000,
LiteLLM http://localhost:4000,
Langfuse http://localhost:3000,
SeaweedFS http://localhost:9333,
Prometheus http://localhost:9090,
Grafana http://localhost:3001
