# Model card: purchase order extraction

Part of the governance pack ([../eu-ai-act.md](../eu-ai-act.md)). Self-assessment for a
portfolio project, not legal advice. Last updated: 2026-10-28.

| | |
|---|---|
| Owner | Abhinav (CODEOWNERS of `prompts/`) |
| Component | `services/extractor` (`extractor.app`), also `POST /extract` |
| Model | LiteLLM alias **`o2c-extract`** -> `openai/gpt-6-luna` (default reasoning effort). Fallback `o2c-extract-previous` -> `openai/gpt-4.1-mini` until 2027-01-06 (`local/litellm/config.yaml`, ADR-008). |
| Prompt | `prompts/extract.md`, last changed in `9e626e7` (2026-10-22). Synced to Langfuse with labels `local` and the git sha (`ml/sync_prompts.py`). |
| Output | `o2c_common.schema.PurchaseOrder` (Decimal and date fields). JSON mode with a field list and a JSON example in the prompt; never the raw schema (ADR-008). |
| Input | Layout-mode text of the PDF (`o2c_common.pdf.pdf_text`, invisible text under 4 pt dropped), wrapped in `<document>` tags (ADR-010) |
| Cost | $0.00035 to $0.00064 per PO (ADR-008); full eval gate run $0.11 |

## Intended use
- Copy the fields of a business purchase order (number, dates, customer, ship-to, lines,
  total) into a structured PO. The agent validates it and either posts it or sends it to
  review.
- **The model decides nothing.** The agent's code checks the result against the ERP:
  - customer number and name;
  - material numbers;
  - prices against the price table and the contract tolerance;
  - quantities against the anomaly model;
  - the total.
- Values that reference master data are never used unchecked (CLAUDE.md).

## Out of scope
- Scanned or image-only PDFs: there is no OCR, so there is no text to extract.
- Handwriting, non-Latin scripts, languages other than German and English.
- Any decision: approving, pricing, credit.

## Data
All synthetic, from `poc/generate_pos.py` (reportlab):
- `poc/data` (golden): 50 POs, 6 layouts, German and English.
- `ml/data/train`: 100 POs, seed 2026.
- `ml/data/holdout`: 20 harder POs, seed 4242. They have two layouts never seen in
  training, two-page orders and noise text. The holdout is used **only for final
  measurement**, never for tuning (CLAUDE.md).

No customer data was used.

## Results
Measured with the production extraction code (`ml/eval_extraction.py`, gate
`ml/eval_gate.py`). The model is not deterministic, so the gate uses thresholds
(ADR-002).

| Run | poc/data perfect POs | poc/data field acc. | holdout perfect POs | holdout field acc. |
|---|---|---|---|---|
| gpt-4.1-mini, 3 runs + holdout once (ADR-008) | 50.00 mean | 100.00% | 9/20 | 99.01% |
| gpt-6-luna, 3 runs + holdout once (ADR-008) | 49.33 mean (49-50) | 99.97% | 13/20 | 99.37% |
| gpt-6-luna, full gate 2026-10-22 (ADR-008) | 50/50 | 100.00% | 10/20 | 99.10% |
| gpt-6-luna, final gate of E11 (E11 guide) | 49/50 | 99.95% | 14/20 | 99.46% |
| **gpt-6-luna, full gate 2026-10-28 (this epic)** | **50/50** | **100.00%** | **11/20** | **99.19%** |
| Gate thresholds (`eval/thresholds.yaml`) | >= 48/50 | >= 99.50% | >= 9/20 | >= 98.80% |

Holdout perfect POs range from 10 to 14 across runs of the same model. That spread is
run-to-run noise, which is why the migration rule compares means and allows one PO of
difference (ADR-008).

## Known errors
- **`customer_number` on the holdout** (every run, 7 to 11 POs; 9 on 2026-10-28):
  - The "formular" layout prints the *supplier* number at the customer instead of the
    customer number.
  - The "portal" layout labels the number "at supplier".
  - A prompt rule only moved the errors between layouts (ADR-005). Fixed **in code**
    instead: `resolve_customer` accepts a number only if the ERP customer with that
    number has the printed name. A number of another real customer raises
    `CUSTOMER_MISMATCH` (ADR-004, ADR-010).
- **Line descriptions:** occasional additions such as "(your item no. ...)" on PO-0030
  (gpt-6-luna, 2 of 3 runs, ADR-008). Material matching tolerates it.
- **Following instructions in the document:** gpt-4.1-mini followed hidden white text
  (RT-01, ADR-010). Now:
  - invisible text is dropped before extraction;
  - `o2c_common.injection.detect` flags instruction-like text, which raises
    `PROMPT_INJECTION_SUSPECTED`;
  - price checks in code catch the rest.
- **Wrapped structured answers:** gpt-6-luna echoed a pasted schema as
  `{"properties": ...}`. Fixed by strict structured outputs or a JSON example at every
  call site. Regression tests: `services/agent/tests/test_structured_output.py`
  (ADR-008).

## Limits
- Synthetic, clean PDFs. Real POs (scans, rotated pages, email bodies) will score lower.
- 50 + 20 POs: one PO moves the rate by 2 to 5 points.
- Depends on OpenAI availability. LiteLLM falls back once to the previous model, and
  Kafka retries, then the DLQ ([../../operations/runbooks.md](../../operations/runbooks.md)).

## Monitoring
- `o2c_extraction_seconds` (p95 panel) and `o2c_llm_cost_usd_total{service="extractor"}`
  with alert `LlmCostHigh` (ADR-012).
- `TouchlessRateLow` and `o2c_issues_total{code}`: a worse extraction shows up as more
  exceptions.
- Every call is a trace in Langfuse, with dataset runs on `golden` and `holdout`
  (ADR-010).
- The full gate runs in every promotion to test (ADR-018).

## Change procedure
A model or prompt change needs:
1. The noise-aware migration rule on poc/data and train, with the holdout once
   (`ml/compare_extraction.py`).
2. A **full gate with `o2c-extract` pointed at the candidate**, covering every LLM call
   site (ADR-008).
3. A PR (CODEOWNERS on `prompts/`).

On the CI side, the PR's `eval` job runs the quick extraction and injection checks
(ADR-015).
