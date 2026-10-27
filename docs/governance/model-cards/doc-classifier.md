# Model card: document classifier (hybrid)

Part of the governance pack ([../eu-ai-act.md](../eu-ai-act.md)). Self-assessment for a
portfolio project, not legal advice. Last updated: 2026-10-28.

| | |
|---|---|
| Owner | Abhinav |
| Component | `services/classifier` (`classifier.hybrid`, consumer group `classifier`) |
| Model | MLflow `o2c-doc-classifier` **v3** `@champion`: `distilbert-base-multilingual-cased` fine-tuned with PyTorch (`ml/train_classifier.py`). Baked into the image: `models.lock.json` pins `ghcr.io/abhinavsingh-a1/o2c-model-doc-classifier:v3@sha256:03dc6735...` (ADR-016). |
| Fallback / double-check | GPT through `o2c-extract` (gpt-6-luna), prompt `prompts/classify.md`, strict structured outputs (ADR-008) |
| Classes | purchase_order, invoice, remittance, other |
| Input | First 1500 characters of the layout-mode text, max 256 tokens |
| Training | CPU, max_length 256, batch 16, AdamW 5e-5, 3 epochs (E10 guide) |

## Intended use
- Decide which incoming documents are purchase orders. Only those go on to extraction.
- **Risk-based routing, decided in code** (`classifier.hybrid.route`, ADR-009):

  | DistilBERT says | Confidence | Who decides |
  |---|---|---|
  | invoice, remittance or other | >= `CLASSIFIER_NON_PO_THRESHOLD` (0.005) | DistilBERT |
  | invoice, remittance or other | below it | GPT |
  | purchase_order | any | **always GPT** (a false PO is the expensive mistake) |
  | (model unavailable) | - | GPT, with a warning |

- `docs.classified` records `classified_by`, `confidence` and `routing_reason` for every
  document.

## Out of scope
- Document types other than the four classes.
- Scans without a text layer.
- Any order decision.

## Data
Synthetic, generated per seed. Tests check that no training PDF is byte-identical to a
holdout PDF.
- `ml/data/classifier/train`: 240 documents (seeds 3101/3102).
- `ml/data/classifier/train_hard`: 240 documents (seeds 3201/3202), with the variety of
  real mail: footers, payment reminders, RFQs, cancellations.
- Validation split: 96 documents, used for the threshold.
- `ml/data/classifier/holdout`: 40 documents, 10 per class (seeds 5101/5102). Measured
  once per model.

## Results (holdout, 40 documents)
| Classifier | Accuracy | False POs | Sent to GPT | ms/doc | Cost / 1000 docs |
|---|---|---|---|---|---|
| GPT only (gpt-4.1-mini) | 97.5% | 0 | 100% | 1,192 | $0.30 |
| DistilBERT v1 (240 docs) | 47.5% | 20 of 21 mistakes were "purchase_order" | 0% | 123 | $0 |
| DistilBERT v3 alone (480 docs) | 100.0% | 0 | 0% | 148 | $0 |
| **Hybrid, risk-based (production)** | **100.0%** | **0** | **25%** | **418** | **$0.11** |
| Full gate 2026-10-28 (gpt-6-luna as GPT) | 100.0% | 0 | | | |

Validation (v3): 97.9%. Its only 2 mistakes were false POs (p = 0.960 and 0.983), which
go to GPT anyway.

Sources: ADR-005, ADR-009, `eval/report.md` of 2026-10-28.

## Known errors and limits
- **v1 did not generalise** (47.5%). It learned the training layouts, not the task. v3
  added varied training documents from training seeds only.
- **100% on 40 synthetic documents** shows that the model learned these templates, not
  that it handles real customer mail. One mistake moves accuracy by 2.5 points.
- **Confidence is compressed** (0.97 to 0.99), so a confidence threshold does not separate
  mistakes. That is why routing goes by risk (ADR-009).
- **Residual failure mode:** a PO that DistilBERT confidently labels as a non-PO is not
  double-checked. The order never arrives. That is visible to the customer, but it is not
  a wrong order. It did not occur on validation or holdout.

## Monitoring
- `o2c_documents_classified_total{doc_type, classified_by}`: a jump in GPT share or in
  "other" is visible on the dashboard.
- The `classifier` key's LLM cost, with budget $1 per month (ADR-011).
- The gate checks hybrid accuracy >= 97.5% and false PO labels = 0 (`eval/thresholds.yaml`).

## Change procedure
1. Train (`ml/train_classifier.py`).
2. Measure the holdout once.
3. Move `@champion` only if within 2 points of GPT (ADR-005).
4. Recompute the threshold on validation (`ml/choose_threshold.py`).
5. `make models-publish`, then a PR with the new `models.lock.json`, then the release and
   promotion (ADR-016).
