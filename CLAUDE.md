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
- Commit messages and PR titles follow Conventional Commits.
- Work on a branch and open a PR; never push to main.
- Write tests for every new function.
- Record important decisions as ADRs in docs/adr/.

## Commands
- make up: start local stack (Postgres, Kafka, SeaweedFS, MLflow, LiteLLM, Langfuse)
- make down: stop it
- make ps / make logs: status and logs
- make buckets: create S3 buckets
- make lint: run all pre-commit checks
- make clean: stop and DELETE all local data

Local URLs: MLflow http://localhost:5000,
LiteLLM http://localhost:4000,
Langfuse http://localhost:3000,
SeaweedFS http://localhost:9333
