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
- Prompts live in prompts/*.md; {schema} is replaced with the JSON schema at startup.
- make send PDF=<path>: send a PDF to intake (the full pipeline runs in Docker via make up)
- make agent: run the agent locally at http://localhost:8400/docs (stop the Compose agent first)
- Review a paused order: GET/POST http://localhost:8400/reviews/{idempotency_key}
- make gen-api: regenerate ui/src/api/schema.d.ts from the agent's OpenAPI (agent must be running)
- Review API: GET /reviews?status=awaiting_review|all|posted, GET /reviews/{key}, GET /reviews/{key}/document, POST /reviews/{key}
- uv run python -m agent.auth add-user <username> "<Full Name>": create a reviewer in local/users.json (git-ignored)

Local URLs: MLflow http://localhost:5000,
LiteLLM http://localhost:4000,
Langfuse http://localhost:3000,
SeaweedFS http://localhost:9333
