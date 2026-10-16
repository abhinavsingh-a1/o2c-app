# Order-to-Cash Agent — Project Plan

Oct 13, 2026 · @Abhinav

## Overview

The project ships in 7 phases over 17 weeks (19 Oct 2026 – 14 Feb 2027), ending with the Order-to-Cash Agent live in a separate Prod AWS account behind a fully audited promotion pipeline.

**Scope:** purchase orders arrive by email, get classified and extracted, are validated against contracts and ERP master data, and are posted as sales orders. The CRM is updated and exceptions go to finance via ServiceNow. Every tool from the project stack is used at least once with a stated reason.

**Assumptions**

- One developer (you) at about 30 h/week, using Claude Code (included in Claude Pro) as a pair programmer.
- The ERP is a Postgres-backed mock with SAP-style tables and REST endpoints. The CRM is a HubSpot free account; ServiceNow is a free Personal Developer Instance (PDI).
- Purchase orders and contracts are synthetic or anonymized, so no real customer data is involved.
- The two weeks over Christmas run at reduced pace and are absorbed in Phase 3.

**How the plan is structured:** each Phase ends in one Milestone with exit criteria you can demo. A Phase holds one or more Epics (E1–E16), and each Epic is a checklist of Tasks sized at roughly half a day to two days. Epics map directly to GitHub Issues/Projects: one Epic = one milestone-labelled issue, Tasks = sub-issues.

&#91;embedded content: roadmap · 7 phases, 7 milestones\]

Each bar is a phase and the diamond at its end is its milestone; M6 on 14 Feb 2027 is the production launch.

## Costs and accounts

Only three things cost money: the two LLM APIs and AWS. Everything else runs on free plans or is self-hosted.

| Item | Where to pay | Expected cost | When |
| --- | --- | --- | --- |
| Claude Pro | Already paid | Included | Whole project: Claude Code for all development |
| OpenAI API (main provider: extraction, fine-tuning, embeddings) | platform.openai.com → Billing (prepaid credits, separate from ChatGPT Plus) | About $5/month; one fine-tuning job costs a few dollars | From Phase 1 |
| Claude API (fallback model in LiteLLM) | Claude Console → Billing (prepaid credits, separate from Claude Pro) | A few cents in testing | From Phase 3 |
| Zapier | Free trial only | $0 if cancelled after Phase 4 | Phase 4 (about 2 weeks) |
| AWS | AWS Billing | Largest cost: each EKS cluster's control plane alone is about $73/month, plus nodes, RDS and MSK | Dev from Phase 0; Test and Prod only in Phases 5–6 |
| GitHub, Docker Desktop, ServiceNow PDI, HubSpot CRM, Langfuse, MLflow, LiteLLM, SeaweedFS | Free (public repos, personal use, developer instance, free CRM, self-hosted) | $0 | Throughout |

**Cost rules**

- Set a monthly spend limit in the OpenAI and Claude Console dashboards, and an AWS Budget with email alerts.
- Destroy cloud environments with Terraform when not working on them; keep only state and images.
- Keep the Anthropic API key in `.env` only, never as a Windows system variable: Claude Code would otherwise use it and bill API rates instead of your Pro plan.
- Zapier: build both Zaps, test them, record the demo, then cancel. Exports and screenshots of each Zap stay in the repo so the project remains complete without a subscription.

## Phase 0 — Foundations (weeks 1–2, 19 Oct – 1 Nov)

**Milestone M0 — Walking skeleton.** Exit criteria: the local stack starts with one command; a hello-world FastAPI service is built by GitHub Actions and deployed to the Dev EKS cluster by ArgoCD; Grafana shows its metrics.

### E1 — Repositories and developer workflow

- [ ] Create `o2c-app`, `o2c-infra` and `o2c-gitops` repos on GitHub
- [ ] Branch protection on `main`: PR required (0 approvals while solo), green checks, linear history
- [ ] CODEOWNERS for `prompts/`, `eval/golden/`, `terraform/`
- [ ] Conventional Commits convention; repos are public
- [ ] pre-commit hooks: gitleaks, ruff, eslint, `terraform fmt`/`validate`, sqlfluff
- [ ] `CLAUDE.md` describing architecture, saga steps, coding rules and test expectations for Claude Code
- [ ] `docs/adr/` folder with ADR-001 (three-repo layout, build once / promote many)

### E2 — Local development environment

- [ ] docker compose: Postgres 16 + pgvector, Kafka (KRaft), Langfuse, LiteLLM, MLflow, SeaweedFS (S3 stand-in; MinIO images are no longer published)
- [ ] Makefile targets: `up`, `down`, `seed`, `test`, `lint`, `eval`
- [ ] `.env.example` with LiteLLM dev key and budget; `.env` git-ignored
- [ ] kind cluster + shared Helm chart smoke-installed locally
- [ ] Python (uv) and TypeScript (pnpm) workspace setup in `o2c-app`

### E3 — Dev platform infrastructure

- [ ] AWS Organizations with Dev, Test and Prod accounts; Terraform S3 remote state with locking
- [ ] Terraform modules: VPC, EKS, RDS PostgreSQL (pgvector enabled), MSK, S3, ECR, Secrets Manager, IAM (IRSA)
- [ ] Apply the Dev environment only
- [ ] Install ArgoCD, External Secrets and kube-prometheus-stack (Prometheus + Grafana) with Helm
- [ ] Ansible playbook: Jenkins controller + agent EC2 on Linux, CIS hardening, auditd
- [ ] GitHub Actions baseline CI: lint, unit tests, Docker build, push to ECR
- [ ] Hello-world FastAPI service deployed to Dev via ArgoCD from `o2c-gitops/envs/dev`

## Phase 1 — Proof of concept (weeks 3–4, 2 Nov – 15 Nov)

**Milestone M1 — PoC demo.** Exit criteria: a notebook classifies, extracts and price-checks 50 sample POs end to end; field-level accuracy and cost per document are measured; a written gap list names what would break in production.

### E4 — Notebook prototype

- [ ] Create 50 synthetic POs in 6+ vendor layouts (PDF), each with ground-truth JSON
- [ ] Create 10 sample customer contracts with price agreements and discount rules
- [ ] Define the PO JSON schema (header, line items, prices, delivery, customer IDs)
- [ ] Prompt-engineered document-type classification (PO / invoice / remittance / other) with GPT
- [ ] Prompt-engineered extraction with structured output; few-shot examples per layout
- [ ] LangChain loaders + splitter; contract chunks embedded into local pgvector
- [ ] RAG price check: retrieve the customer's price clause, compare to extracted prices
- [ ] Trace every call to local Langfuse; record tokens and cost per document
- [ ] Scoring script: field-level accuracy against ground truth (baseline for later eval gates)
- [ ] ADR-002 "PoC findings": accuracy, cost, failure modes, and the production gap list (no retries, no idempotency, no audit, no human review)

## Phase 2 — MVP services (weeks 5–8, 16 Nov – 13 Dec)

**Milestone M2 — End to end in Dev.** Exit criteria: a PO PDF posted to the intake webhook becomes a sales order in the ERP mock; a failing PO lands in the review UI; killing a pod mid-saga resumes without double-posting.

### E5 — ERP mock and data model

- [ ] SQL schema in SAP style: customers (KNA1), materials (MARA), price conditions, sales order header/items (VBAK/VBAP)
- [ ] Alembic migrations; seed script with customers, materials and price lists
- [ ] FastAPI ERP mock with OData-style REST endpoints (read master data, create/cancel sales order)
- [ ] Order history table used later by the anomaly model

### E6 — Event backbone

- [ ] Kafka topics: `docs.received`, `docs.classified`, `orders.extracted`, `orders.validated`, `orders.exceptions`, `orders.posted`, `orders.dlq`
- [ ] Event envelope with JSON Schema validation, correlation ID and idempotency key
- [ ] Shared consumer library: retries with backoff, DLQ after N failures, idempotent processing table
- [ ] DLQ replay CLI

### E7 — Intake and extraction services

- [ ] Flask intake webhook: HMAC check, file validation, upload to S3, produce `docs.received`
- [ ] FastAPI extraction service: classify, extract with LangChain structured output parser, produce `orders.extracted`
- [ ] All LLM calls routed through LiteLLM (no OpenAI or Anthropic keys in services)
- [ ] Unit tests with recorded LLM responses (no live calls in CI)

### E8 — Validation and posting agent (LangGraph)

- [ ] Graph nodes: validate customer (SQL tool) → price check (RAG tool) → anomaly check (stub) → post order (ERP REST) → emit `orders.posted`
- [ ] Postgres checkpointer so a crashed run resumes from the last node
- [ ] Saga compensation: cancel the ERP order if a later step fails
- [ ] Human-in-the-loop interrupt: exceptions pause the graph and emit `orders.exceptions`
- [ ] Plain-language exception explanation prompt for finance staff

### E9 — Exception review UI

- [ ] React + TypeScript app: exception queue, side-by-side PDF viewer and extracted fields
- [ ] Approve / edit / reject actions call the FastAPI resume endpoint
- [ ] OIDC login; every action records user, timestamp and before/after values
- [ ] Helm chart values for all services; deploy to Dev via ArgoCD

## Phase 3 — ML and LLMOps (weeks 9–11, 14 Dec – 3 Jan)

**Milestone M3 — Models governed by evals.** Exit criteria: the fine-tuned OpenAI extractor beats the PoC baseline on the golden set; all models are versioned in MLflow and promoted by alias; one command runs the full eval suite and fails below threshold. This phase covers the holidays, so it carries less scope per week.

### E10 — Models, fine-tuning and training pipelines

- [ ] Build a JSONL fine-tuning set from corrected extractions (train/validation split, no golden-set leakage) for the OpenAI fine-tune and the PyTorch classifier
- [ ] OpenAI fine-tuning job for PO extraction; compare the fine-tuned model against the base model on accuracy, latency and cost
- [ ] PyTorch: fine-tune DistilBERT document-type classifier; replace prompt-based classification
- [ ] scikit-learn: IsolationForest anomaly model on quantity and amount vs customer history; wire into the agent's anomaly node
- [ ] Kubeflow Pipelines on EKS: data prep → train → evaluate → register (one pipeline per model)
- [ ] MLflow tracking + model registry; aliases `@dev`, `@test`, `@prod`
- [ ] Helm values pin model versions; a retrain opens a promotion PR in `o2c-gitops` instead of deploying

### E11 — LLM gateway and evaluation

- [ ] LiteLLM config: fine-tuned OpenAI model as primary, base OpenAI model on low confidence, Claude as cross-provider fallback when OpenAI is down or too slow, OpenAI embeddings; virtual key and budget per service
- [ ] Langfuse golden dataset (PoC set + new hard cases); field-level accuracy scorer
- [ ] LLM-as-judge scorer for exception explanations (clarity, correctness, cites evidence)
- [ ] Prompts stored in Git as source of truth and synced to Langfuse with environment labels
- [ ] Red-team set: POs with hidden prompt-injection text, spoofed customers, impossible discounts
- [ ] `make eval` script returning non-zero below thresholds, ready for CI

## Phase 4 — Business integrations (weeks 12–13, 4 Jan – 17 Jan)

**Milestone M4 — Real systems connected.** Exit criteria: an email with a PO attachment flows into the pipeline without manual steps; posted orders appear in the CRM; exceptions open a ServiceNow case, and resolving that case resumes the agent.

### E12 — Email and CRM automation (Zapier)

- [ ] Zap: new Outlook/Gmail email with PDF attachment → Code step (JavaScript) signs the payload with HMAC → Flask intake webhook
- [ ] Consumer on `orders.posted` calls a Zapier catch hook → HubSpot deal and line items updated
- [ ] Zap error handling: failed runs alert by email; idempotency key prevents duplicates on Zap retries
- [ ] Run Zapier on the free trial only (build both Zaps, test, record the demo, then cancel); keep each Zap's export, screenshots and setup (trigger, steps, owner) in `docs/integrations/`

### E13 — ServiceNow exception and change workflows

- [ ] ServiceNow PDI: custom "Order Exception" table and assignment rules for the finance group
- [ ] Consumer on `orders.exceptions` creates the case via REST Table API with reason, evidence links and agent explanation
- [ ] Business Rule (JavaScript) on case resolution calls back the FastAPI resume endpoint
- [ ] Change Request template and REST call prepared for Prod deployments (used in Phase 5)
- [ ] Integration tests against the PDI, run from Jenkins in Test

## Phase 5 — CI/CD, audit gates and environments (weeks 14–15, 18 Jan – 31 Jan)

**Milestone M5 — Audited promotion to Prod.** Exit criteria: one signed image digest moves Dev → Test → Prod via Git commits only; every audit gate blocks on a seeded failure; a Prod deploy cannot start without an approved ServiceNow Change Request.

### E14 — Pipelines and environment promotion

- [ ] Terraform: apply the Test and Prod accounts from the same modules (env-specific tfvars)
- [ ] Jenkins Terraform pipeline: plan → Checkov on the plan → manual approval → apply the saved plan
- [ ] GitHub Actions PR gates: Semgrep, pip-audit, npm audit, Checkov (Terraform + Helm), Trivy, `dbt build`, migration check, `make eval`
- [ ] On merge: Syft SBOM, Cosign signing, build provenance attestation, push to ECR with immutable tags
- [ ] Kyverno policies in every cluster: signed images from own ECR only, no root, no `latest`, resource limits
- [ ] ArgoCD ApplicationSet for dev/test/prod; Dev auto-syncs, Test and Prod sync after approved PRs
- [ ] Jenkins promotion pipeline to Test: open `o2c-gitops` PR with the digest → integration tests → OWASP ZAP → k6 load test → red-team eval
- [ ] Jenkins promotion to Prod: create ServiceNow CR with reports attached → wait for CR approval → `input` step by a non-author approver → merge → smoke tests → Prometheus SLO check → auto `git revert` on breach
- [ ] Dependabot for Python, npm, Docker and Terraform providers
- [ ] Seed a failure for each gate once and record evidence that it blocked

## Phase 6 — Production readiness and launch (weeks 16–17, 1 Feb – 14 Feb)

**Milestone M6 — Production launch.** Exit criteria: Prod processes the full sample stream for a week with dashboards and alerts live; an auditor walkthrough traces one order from Langfuse trace to its commit; the portfolio README and demo video are published.

### E15 — Observability, BI and runtime audit

- [ ] Prometheus metrics in every service: straight-through-processing rate, latency per stage, Kafka consumer lag, LLM cost
- [ ] Grafana dashboards + alert rules (DLQ growth, SLO burn, budget near limit)
- [ ] Kafka → Postgres sink for the analytics schema
- [ ] dbt models and tests: touchless rate, exception reasons, cost per document, time to resolve exceptions; shown in Grafana
- [ ] CloudTrail and AWS Config in all accounts; EKS control-plane audit logs on
- [ ] Jenkins Audit Trail plugin; GitHub audit log export
- [ ] Kafka → S3 audit sink with Object Lock (WORM) for every order decision and human override

### E16 — Governance, operations and launch

- [ ] EU AI Act assessment: risk classification with reasoning, transparency and human-oversight measures
- [ ] Data protection: data map, retention policy for documents and traces, PII handling in Langfuse
- [ ] Model cards stored in MLflow for each model
- [ ] Human-approval thresholds by order amount, configurable per environment
- [ ] Runbooks: DLQ replay, model rollback, LLM provider outage (fallback), stuck saga
- [ ] Disaster recovery test: restore RDS snapshot into Test and replay Kafka
- [ ] Auditor walkthrough document following one order end to end
- [ ] Architecture diagram, portfolio README, 5-minute demo video

## Costs, accounts and tool decisions

**Paid services**

| Service | Cost | How it is used |
| --- | --- | --- |
| Claude Pro | Already paid | Claude Code writes, tests, reviews and refactors code throughout; the Claude app is used for planning, docs and debugging. Not used as the app's LLM provider. |
| OpenAI API | About $5/month, pay-as-you-go at platform.openai.com (separate from ChatGPT Plus) | Main provider for classification, extraction and fine-tuning. One fine-tuning job costs a few dollars. Monthly budget limit set in the OpenAI project. |
| Zapier | Free trial only, about 2 weeks during Phase 4 | Build both Zaps, test them, record the demo, then cancel. Zap setup stays documented in `docs/integrations/` so the project remains complete without a subscription. |
| AWS | Largest cost; depends on how long resources run | Dev only until Phase 5; Test and Prod applied briefly in Phases 5–6, then destroyed. Budget alerts in every account. |

**Free services:** GitHub (public repos and Actions), Docker Desktop (personal use), ServiceNow Personal Developer Instance, HubSpot free CRM, and self-hosted Langfuse, LiteLLM and MLflow.

**Changes to the original tool list**

- Nothing from the original list has been removed.
- "LightLLM" is interpreted as **LiteLLM**, the LLM gateway.
- "Vector DB" is implemented as **pgvector** inside PostgreSQL.
- Added for security and audit gates: gitleaks, Semgrep, pip-audit/npm audit, Checkov, Trivy, Syft, Cosign, Kyverno, OWASP ZAP, k6, CloudTrail, AWS Config.
- Added for developer workflow: uv, pre-commit, ruff, sqlfluff, Alembic, kind.
- Local S3 stand-in is **SeaweedFS**, because MinIO images are no longer published.
- Named services behind generic items: HubSpot (CRM), ServiceNow PDI, Microsoft Graph not used in this project.

## Definition of done and risks

**A task is done when** it is merged via a reviewed PR with green gates, has tests, is deployed to Dev through ArgoCD, and any new decision is recorded as an ADR.

**An epic is done when** all its tasks are done and its part of the milestone exit criteria has been demonstrated in Dev (or Test/Prod for Phases 5–6).

| Risk | Impact | Mitigation |
| --- | --- | --- |
| AWS cost overrun (EKS, MSK, RDS in 3 accounts) | Budget burn before launch | AWS Budgets alerts; Test and Prod applied only from Phase 5; scale node groups to zero overnight; MSK Serverless or Kafka on EKS for Dev |
| Kubeflow setup consumes Phase 3 | Fine-tuning work slips | Time-box install to 2 days; fall back to Kubeflow Pipelines standalone |
| OpenAI fine-tune does not beat the base model | M3 exit criterion fails | Grow the training set from corrected exceptions; keep the base model if the gain is not worth it and record why in an ADR |
| ServiceNow PDI hibernates after inactivity | Integration tests fail | Wake the instance in the Jenkins pipeline's first step; record API responses as fixtures |
| Synthetic POs are too clean | Accuracy looks better than reality | Add scanned, rotated and multi-page POs to the golden set |
| Scope creep from 30+ tools | Late launch | Phase exit criteria are fixed; anything extra goes to a backlog for after M6 |
