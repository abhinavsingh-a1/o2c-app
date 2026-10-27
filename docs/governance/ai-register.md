# AI register

The inventory of every AI component in o2c-app. Self-assessment for a portfolio project,
not legal advice. Last updated: 2026-10-28. Owner of every row: Abhinav.

How to read the columns:
- **Risk** is the component's own operational risk. The legal classification of the
  whole system is in [eu-ai-act.md](eu-ai-act.md): not high-risk.
- **Logs:** every LLM call through LiteLLM is traced in Langfuse and recorded in
  LiteLLM's spend log with the calling key (ADR-010, ADR-011).
- **Audit archive:** every agent decision and every review is in the hash-chained archive
  (ADR-014).

| # | Component | Purpose | Model / version | Where | Decides | Risk | Human oversight | Logs and evidence | Card |
|---|---|---|---|---|---|---|---|---|---|
| 1 | PO extraction | PDF text to structured PO | `o2c-extract` -> gpt-6-luna; fallback gpt-4.1-mini (until 2027-01-06); prompt `prompts/extract.md` @ `9e626e7` | `services/extractor` | Nothing: code checks every value against the ERP | Medium: wrong values. Mitigated by ERP checks, the injection detector and the red team. | Any issue goes to review; the reviewer sees the PDF next to the extracted values | Langfuse `extract` traces; `orders.extracted`; `o2c_extraction_seconds`; gate | [extraction](model-cards/extraction.md) |
| 2 | Document classifier | PO vs invoice, remittance, other | `o2c-doc-classifier` v3 (DistilBERT) `@champion`, baked by digest; GPT double-check for every PO | `services/classifier` | Routing only. A false PO is double-checked; a missed PO is visible as a missing order. | Low to medium | Indirect: misrouted POs surface as customer follow-ups; `classified_by` in every event | `docs.classified` (`classified_by`, `confidence`, `routing_reason`); `o2c_documents_classified_total`; gate | [doc-classifier](model-cards/doc-classifier.md) |
| 3 | Quantity anomaly | Flag unusual quantities | `o2c-quantity-anomaly` v3 (IsolationForest) `@champion`, baked by digest | `agent.anomaly` | Can only send an order to review (`QUANTITY_ANOMALY` above 0.8) | Medium when silent (incident 2026-10-27) | The flagged order goes to review | `o2c_anomaly_scoring_failures_total` plus a critical alert; gate step "anomaly model scores"; MLflow promotion tags | [quantity-anomaly](model-cards/quantity-anomaly.md) |
| 4 | Material matching | Pick the ERP material for a line without a number when ERP search is ambiguous | `o2c-extract`; `prompts/match_material.md`; strict structured outputs | `agent.materials` | Proposes a number. It is rejected unless it is in the ERP candidate list (`test_material_matcher_rejects_invented_number`). | Low | Unknown material means review; approve is refused while a material is unknown | Langfuse `match_material` traces | - |
| 5 | Contract tolerance (RAG) | Read the price tolerance from contract text | `o2c-extract` + `o2c-embed` (text-embedding-3-small), pgvector; `prompts/tolerance.md` | `agent.contracts` | Supplies a tolerance percentage with its evidence. The price comparison is code. | Low to medium (a wrong tolerance) | The tolerance and its evidence reach the reviewer in the issue | Langfuse `tolerance` traces | - |
| 6 | Exception explanation | Plain-language reason for the reviewer | `o2c-extract`; `prompts/explain_exception.md` @ `a5a7db6` | `agent.explain` | Nothing. On failure the issue list is shown instead. | Low: a misleading text. Judged 14/14 (2026-10-28). | Shown together with the coded issue list and the PDF | Langfuse `explain_exception` traces with judge scores | - |
| 7 | Injection detector | Flag instruction-like text in documents | Rules (regex), not ML: `o2c_common.injection` | extractor, agent | Raises `PROMPT_INJECTION_SUSPECTED`, so a human always reviews | Low (0 false positives on 200 documents) | Always review | `injection_findings` in `orders.extracted`; gate | - |
| 8 | LLM judge (evaluation only) | Score explanation quality | `o2c-judge` -> gpt-5.6-terra; `prompts/judge_explanation.md` | `ml/judge_explanations.py` | Nothing in production. Pass/fail is code (every criterion >= 4). | Low | Validated on 7 calibration cases before every run | `ml/results/judge-explanations*.json`; Langfuse scores | - |

## Not AI (decided by code)
These rules are deterministic and unit-tested:
- whether an order is posted: no issues and total below `auto_post_limit` (10,000 EUR,
  ADR-004);
- every price, total and rounding calculation;
- classifier routing;
- the anomaly threshold;
- the model promotion rules;
- the four-eyes check.

## Change log
| Date | Change | Reference |
|---|---|---|
| 2026-10-21 | DistilBERT v3 champion; anomaly v3 champion | ADR-005, ADR-006 |
| 2026-10-22 | `o2c-extract` gpt-4.1-mini -> gpt-6-luna; hybrid classifier; per-service keys | ADR-008, ADR-009, ADR-011 |
| 2026-10-23 | Models baked into images by digest (`models.lock.json`) | ADR-016 |
| 2026-10-27 | pandas pin after the silent anomaly fallback | ADR-006 |
