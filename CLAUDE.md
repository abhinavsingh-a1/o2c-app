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
(Added in E2 when the local stack exists.)
