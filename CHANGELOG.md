# Changelog

By epic (project plan in `docs/epics/`). Dates are when the work was merged to `main`.
Only the local parts exist so far; AWS (E3) and the business integrations (E12, E13)
are not built.

## E16: Governance and operations (local parts), 2026-10-28
- Agent ERP client retries reads (GET) on timeouts, connection errors and 502/503/504:
  3 attempts with exponential backoff and jitter. Creates and cancels are never retried.
  New metric `o2c_erp_retries_total{call}`.
- `ml/judge_explanations.py` retries a failed PO once, then records it as `error` (not
  passed) instead of failing the judge step.
- Governance pack `docs/governance/`: EU AI Act assessment (not high-risk), DPIA, model
  cards, AI register, risk register, human oversight, STRIDE threat model.
- Operations pack `docs/operations/`: runbooks, SLOs, DR plan.
- `make audit-check` (`scripts/audit_check.sh`): verifies the images in prod (or test),
  lists promotion PRs, shows the Jenkins audit trail and verifies the audit chain.
- README, SECURITY.md and this changelog.

## E15: Observability, analytics, audit trail, 2026-10-22
- Prometheus metrics in every service, Grafana "O2C Operations" and alert rules (ADR-012).
- Analytics sink, dbt marts and the "O2C Business" dashboard; cost per order by session
  (ADR-013).
- Hash-chained audit archive in `s3://o2c-audit` with `ml/verify_audit.py`; AWS audit
  module written, not applied (ADR-014).
- Fixed: ERP mock rounding (half-even vs half-up), found by the saga.

## E14: CI/CD, supply chain, GitOps, promotion, 2026-10-22 to 2026-10-27
- PR gates and Dependabot (ADR-015); gate evidence PR #63, which led to gitleaks over
  every PR commit.
- Release: build once, Trivy, SBOM, provenance, cosign keyless, models baked by digest
  (ADR-016).
- kind platform: Argo CD, Kyverno and per-environment topic prefixes (ADR-017).
- Jenkins via Ansible: promote-to-test and promote-to-prod with ZAP, k6, eval, four-eyes
  approval and automatic rollback (ADR-018).
- Fixed:
  - the pandas 3 drift that silently disabled the anomaly model (ADR-006);
  - MLflow worker timeouts;
  - Argo CD DNS on kind;
  - security headers.

## E11: LLM gateway, evaluation, quality gate, 2026-10-22
- `o2c-extract` migrated to gpt-6-luna by a noise-aware rule plus a full gate (ADR-008).
- Hybrid classifier with risk-based routing (ADR-009).
- Langfuse datasets, LLM-as-judge and red team: 6/8, then 8/8 (ADR-010).
- `make eval` and `make eval-quick`; per-service LiteLLM keys and budgets (ADR-011).

## E10: Models and training pipelines, 2026-10-21
- Training, held-out and classifier datasets; fine-tuned DistilBERT classifier (ADR-005).
- IsolationForest quantity anomaly model in MLflow (ADR-006); Kubeflow pipeline with a
  promotion rule (ADR-007).

## E9: Exception review UI (milestone M2), 2026-10-20 to 2026-10-21
- React review queue and detail page with the PDF viewer.
- Approve, edit and reject with exact money maths.
- Login, protected review API and the append-only decision audit.

## E8: Validation and posting agent, 2026-10-20
- ERP tools, contract-tolerance RAG and material matching.
- LangGraph graph with saga compensation (ADR-004).
- Postgres checkpoints and human-in-the-loop interrupt.

## E7: Intake, classification, extraction, 2026-10-20
Flask intake webhook, classifier consumer and FastAPI extractor, all in Docker Compose.

## E6: Event backbone, 2026-10-19
Envelope events, idempotent `EventConsumer`, DLQ and replay, topics (ADR-003).

## E5: ERP mock and data model, 2026-10-19
SAP-style tables, REST API, master data and order history.

## E4: Proof of concept (milestone M1), 2026-10-17
Notebook PoC on 50 synthetic POs: 49/50 perfect with layout-mode text (ADR-002).

## E3: Dev platform infrastructure on AWS
Not started. Terraform bootstrap and the audit module exist in o2c-infra and are not
applied. The RDS restore test belongs here ([dr-plan](docs/operations/dr-plan.md)).

## E2: Local development environment, 2026-10-15 to 2026-10-16
Docker Compose stack, Makefile, uv and pnpm workspaces, kind.

## E1: Repositories and developer workflow, 2026-10-14
Three repositories (ADR-001), CODEOWNERS, pre-commit, Conventional Commits, CLAUDE.md.

## E12, E13: Zapier/CRM and ServiceNow
Not started. The promotion pipeline records the change request as `SKIPPED (E13 pending)`.
