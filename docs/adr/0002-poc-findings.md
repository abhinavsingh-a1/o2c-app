# ADR-002: PoC findings

Date: 2026-10-17
Status: Accepted

## Context
Notebook PoC on synthetic data: 50 POs (6 layouts, German/English), 30 non-PO documents,
8 customer contracts. All LLM calls via LiteLLM to OpenAI (gpt-4.1-mini), traced in Langfuse.

## Results
| Run | Perfect POs | Field accuracy | Tokens/PO | Cost/PO |
|---|---|---|---|---|
| Baseline prompt | 3/50 | 96.5% | 1363 | $0.00111 |
| v2: business context (supplier vs customer, address format) | 44/50 | 99.7% | 1491 | $0.00116 |
| v3: material-number rule | 46/50 | 99.8% | 1491 | $0.00111 |
| v3 + pypdf layout-mode text | 49/50 | 99.9% | 1513 | $0.00117 |

- Classification (PO / invoice / remittance / other): 80/80.
- RAG price check on ground-truth POs: 50/50 (10 deviations found, 0 false alarms).

## Decision
- Keep gpt-4.1-mini behind the LiteLLM alias `o2c-extract`; fine-tuning is evaluated in Phase 3, not needed to pass the PoC.
- Measure "perfect POs" (touchless rate) as the primary metric, not field accuracy.
- Use layout-mode PDF text extraction.
- LLM extracts values; Python does all arithmetic and threshold decisions.
- Production price lookup comes from ERP price tables (SQL); RAG is used for contract wording (tolerances, terms).

## Findings
- Business context in the prompt mattered more than anything else (3 -> 44 perfect POs).
- Input quality beats prompt complexity: layout-mode text fixed wrapped table cells.
- The golden set itself had a bug (clipped descriptions); a test now checks ground truth against PDF text.
- GPT is not fully deterministic at temperature 0; evaluation gates must use thresholds.
- Contract tables split across RAG chunks; exact lookups via RAG are fragile.

## Gap list (what breaks in production)
- Notebooks, not services; no retries, idempotency, dead-letter queue or crash recovery.
- No human review for exceptions; no audit trail beyond Langfuse traces.
- Prompts tuned on the same 50 POs they are measured on; a held-out set is needed.
- Synthetic, clean PDFs only; no scans, multi-page POs or email bodies.
- No prompt-injection defence (instructions hidden in a PDF).
- No PII handling or data-retention rules for documents and traces.
- No CI evaluation gate; results are produced by hand.
