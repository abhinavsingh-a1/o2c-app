# Risk register

Self-assessment for a portfolio project, not legal advice. Last updated: 2026-10-28.
Owner: Abhinav. Reviewed with every epic and after every incident.

Scales:
- **Likelihood (L) and impact (I):** 1 low, 2 medium, 3 high.
- **Inherent risk:** before the controls.
- **Residual risk:** with the controls listed.

Data-protection risks to data subjects are in [dpia.md](dpia.md) §7. Security threats are
in [threat-model.md](threat-model.md).

| # | Risk | L | I | Controls (code, tests, gates) | Residual |
|---|---|---|---|---|---|
| R-01 | A wrong order is posted (wrong price, quantity, customer, material) | 2 | 3 | <ul><li>Code decides, not the LLM (ADR-004): `agent.graph` checks the customer, materials, prices with contract tolerance, quantities, the total and the auto-post limit.</li><li>Tests: `services/agent/tests/test_graph.py`.</li><li>`verify_order` plus `cancel_order` saga (`test_verify_mismatch_cancels_order`).</li><li>Red team 8/8 (`ml/eval_redteam.py`, gate).</li></ul> | Low |
| R-02 | Prompt injection in a PDF manipulates extraction or approval | 2 | 3 | <ul><li>Invisible text dropped (`o2c_common.pdf`, `test_hidden_redteam_injection_is_dropped`).</li><li>`o2c_common.injection` raises PROMPT_INJECTION_SUSPECTED, so the order always goes to review (`test_injection_findings_always_need_review`).</li><li>The LLM cannot approve.</li><li>Red team RT-01 to RT-03.</li></ul> | Low. White text at normal size and text in images are not detected; the price and total checks still apply. |
| R-03 | An LLM invents master data (a material number, a customer) | 2 | 2 | <ul><li>Matches must come from the ERP candidates (`test_material_matcher_rejects_invented_number`, `test_invented_material_match_is_unknown_material`).</li><li>A customer number is accepted only with a matching name (`test_look_alike_number_with_unknown_name_is_not_trusted`).</li></ul> | Low |
| R-04 | A model silently stops working (fallback hides it) | 2 | 3 | <ul><li>`o2c_anomaly_scoring_failures_total` plus the alert `AnomalyScoringFailing`.</li><li>The gate step "anomaly model scores".</li><li>A pin test for pandas<3.</li><li>Incident 2026-10-27 (ADR-006).</li></ul> | Low |
| R-05 | A model or prompt change degrades quality | 2 | 2 | <ul><li>`make eval` thresholds (`eval/thresholds.yaml`) in every promotion to test (ADR-018).</li><li>The PR `eval` job for prompt and extractor changes (ADR-015).</li><li>The migration rule covers every call site (ADR-008).</li><li>CODEOWNERS on `prompts/`.</li></ul> | Low |
| R-06 | Tuning on the holdout overstates quality | 2 | 2 | <ul><li>Rule in CLAUDE.md; holdout measured once per model (ADR-005, ADR-008).</li><li>Tests check that training and holdout PDFs never overlap (classifier).</li></ul> | Low |
| R-07 | The LLM provider is down or the budget is exhausted | 2 | 2 | <ul><li>LiteLLM fallback `o2c-extract -> o2c-extract-previous` (until 2027-01-06).</li><li>Kafka retries and the DLQ.</li><li>Per-service budgets and the `LlmCostHigh` alert (ADR-011, ADR-012).</li><li>Runbook ([../operations/runbooks.md](../operations/runbooks.md)).</li></ul> | Medium: one provider. A second provider (Claude via LiteLLM) is in the plan, not configured. |
| R-08 | gpt-4.1-mini shutdown (2027-01-14) breaks the fallback | 3 | 1 | Remove `o2c-extract-previous` by 2027-01-06; point the fallback at `o2c-extract-candidate-low` (ADR-008) | Low, if done on time |
| R-09 | A reviewer over-trusts the AI explanation (automation bias) | 2 | 2 | <ul><li>The coded issue list and the PDF are shown next to the explanation (DetailPage).</li><li>The judge checks faithfulness (14/14 on 2026-10-28).</li><li>Reject requires a comment (`test_reject_without_comment_returns_422`).</li></ul> | Medium: the UI does not label the explanation as AI-generated (open item) |
| R-10 | A decision cannot be proven later (audit gap or tampering) | 1 | 3 | <ul><li>Append-only `review_decisions`; the reviewer is taken from the token (`test_decision_records_logged_in_user_as_reviewer`).</li><li>Hash chain plus `ml/verify_audit.py` (`services/audit-archiver/tests`, `ml/tests/test_verify_audit.py`).</li><li>`make audit-check`.</li></ul> | Low locally; Object Lock on AWS is not applied |
| R-11 | An unverified image reaches an environment | 1 | 3 | <ul><li>Keyless cosign, SLSA provenance and SBOM (ADR-016).</li><li>Kyverno verify-image-signatures, registry, digest, non-root and limits policies (ADR-017).</li><li>Promotion verifies before the PR (ADR-018).</li><li>`make audit-check`.</li></ul> | Low |
| R-12 | A bad release in prod | 2 | 2 | <ul><li>Four-eyes approval.</li><li>Smoke test and SLO check.</li><li>Automatic revert PR (ADR-018; exercised twice: #20, #22).</li></ul> | Low |
| R-13 | A secret is leaked in the public repo | 2 | 3 | <ul><li>`.env` git-ignored.</li><li>gitleaks pre-commit and over every PR commit (ADR-015, gate evidence).</li><li>Trivy and Semgrep secrets.</li><li>Rotation runbook.</li></ul> | Low |
| R-14 | A vulnerable dependency | 2 | 2 | <ul><li>pip-audit, pnpm audit, Trivy fs and image scans, Dependabot (ADR-015).</li><li>Ignores expire 2026-12-31.</li></ul> | Medium: 35 pip-audit ignores tied to the MLflow 2.17 / kfp pins |
| R-15 | Dependency updates break models (Dependabot majors) | 2 | 2 | <ul><li>Dependabot ignores with reasons.</li><li>Pin tests.</li><li>The eval gate in promotion (ADR-006, ADR-015).</li></ul> | Low |
| R-16 | Duplicate processing creates two orders | 2 | 3 | <ul><li>Idempotency keys; `EventConsumer` processed-events store (CLAUDE.md).</li><li>ERP idempotent on the key (ADR-004).</li><li>Checkpointed agent (`run_or_resume`).</li><li>Tests: `test_same_event_twice_gives_identical_event_ids`, `test_run_or_resume_on_waiting_order_changes_nothing`.</li></ul> | Low |
| R-17 | Transient ERP errors fail orders or evaluations | 2 | 1 | <ul><li>ERP GETs retried 3 times with backoff and jitter; POSTs are never retried (`agent.erp`, E16, `o2c_erp_retries_total`).</li><li>Consumer retries and the DLQ.</li><li>The judge retries one PO once, then records it as an error (E16).</li></ul> | Low |
| R-18 | Personal data kept too long (Langfuse, raw events, PDFs) | 3 | 2 | Restricted BI role (ADR-013); see the DPIA | Medium until retention jobs exist ([dpia.md](dpia.md) D2, D3) |
| R-19 | The environments share infrastructure (Kafka, Postgres, ERP) | 3 | 1 | <ul><li>Topic and group prefixes per environment (ADR-017).</li><li>test and prod scale to 0 when idle.</li></ul> | Medium locally, by design; isolation comes with the cloud |
| R-20 | The DR plan is untested on AWS | 3 | 2 | Local backup and restore steps ([../operations/dr-plan.md](../operations/dr-plan.md)) | Medium: the RDS snapshot restore test is still to be executed in the AWS session (E3) |
| R-21 | The ServiceNow change request is not enforced for prod | 3 | 1 | Recorded as `SKIPPED (E13 pending)`; four-eyes approval still enforced (ADR-018) | Medium until E13 |
