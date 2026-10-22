# ADR-009: Hybrid document classifier (DistilBERT first, GPT for risky cases)

Date: 2026-10-21
Status: Accepted (risk-based routing; replaces the confidence-only routing, kept below
as history)

## Context
ADR-005 compared the GPT classifier with the fine-tuned DistilBERT on the 40-document
holdout: DistilBERT v3 100.0% at 148 ms and no per-document cost, GPT 97.5% at 1,192 ms
and $0.30 per 1000 documents. The classifier service still used GPT for everything.

The two kinds of mistake do not cost the same. Only documents classified as
`purchase_order` go on to extraction and can become an ERP order. A non-PO document
labelled `purchase_order` (a false PO) could create a wrong order. A PO labelled as
something else fails visibly: the order never arrives and the customer chases it. Labels
among invoice, remittance and other create nothing.

## Decision
- **Model.** `o2c-doc-classifier` v3 (train + train_hard, 100% on the held-out set)
  has the MLflow alias `@champion`.
- **Service.** `classifier.hybrid` loads `models:/o2c-doc-classifier@champion` once at
  start (`MLFLOW_TRACKING_URI`, default http://localhost:5000; Docker Compose sets
  http://mlflow:5000). It downloads the model artifacts and loads them with transformers
  (`model/`, `components/tokenizer/`), using the training input: the first 1500 characters
  of the layout-mode text, max 256 tokens. Restart the classifier to load a new champion.
- **Risk-based routing**, decided in code (`classifier.hybrid.route`):

  | DistilBERT says | Confidence | Decides | `routing_reason` |
  |---|---|---|---|
  | invoice / remittance / other | >= `CLASSIFIER_NON_PO_THRESHOLD` | DistilBERT | `non_po_confident` |
  | invoice / remittance / other | below it | GPT | `low_confidence` |
  | purchase_order | any | GPT | `po_double_check` |
  | (model unavailable at start) | - | GPT, with a warning | `model_unavailable` |

  A wrong non-PO label cannot create an order, so DistilBERT may decide it alone. A
  purchase order is always double-checked, because a false PO is the expensive mistake.
- **Event.** `docs.classified` carries `classified_by` ("distilbert" or "gpt"),
  `confidence` (DistilBERT's softmax confidence; null if the model is unavailable) and
  `routing_reason`.
- **Image.** torch comes from the CPU-only PyTorch index on Linux (`[tool.uv.sources]` in
  the root pyproject; Windows and macOS use PyPI, which are CPU builds). The classifier
  image is 1.63 GB, 711 MB of it torch, with no CUDA libraries.

## Non-PO threshold
Chosen on the **validation split** of `ml/train_classifier.py` (same datasets and seed:
96 documents) with `ml/choose_threshold.py`: the lowest value at which every non-PO
prediction on validation is correct, plus a margin of 0.005. The holdout was not used.

- v3's 70 non-PO predictions on validation are all correct (lowest confidence 0.925). Its
  only 2 mistakes are false POs (other → purchase_order, p=0.960 and 0.983), which go to
  GPT anyway.
- So the lowest safe value is 0.0 and the **default threshold is 0.005**. That is below
  0.25, the lowest top score a 4-class softmax can produce, so for v3 the threshold does
  not filter anything. It is kept as a setting and recomputed per champion: it starts to
  matter as soon as a model makes a non-PO mistake on validation.
- Coverage on validation: DistilBERT decides **72.9%** of documents alone, 27.1% go to GPT
  (every PO prediction).

## Holdout (measured once per routing, `ml/eval_hybrid.py`)

| Classifier | Accuracy | False POs | Sent to GPT | ms / doc | Cost / 1000 docs |
|---|---|---|---|---|---|
| GPT only (ADR-005) | 97.5% | 0 | 100% | 1,192 | $0.30 |
| DistilBERT only (ADR-005) | 100.0% | 0 | 0% | 148 | $0 |
| Hybrid, confidence >= 0.988 (history) | 97.5% | 0 | 87.5% | 944 | $0.27 |
| **Hybrid, risk-based (current)** | **100.0%** | **0** | **25.0%** | **418** | **$0.11** |

- Risk-based routing: 30 documents `non_po_confident` (all correct), 10 `po_double_check`
  (all 10 purchase orders, GPT confirmed each). No false purchase_order labels.
- Compared with GPT alone: the same or better accuracy, 64% less cost, 65% less time per
  document. Each purchase order still costs one GPT call, so the cost scales with the PO
  share of the incoming mail (25% in this holdout).
- GPT is `o2c-extract`, still gpt-4.1-mini (ADR-008).

## History: confidence-only routing (first E11 version)
The first version sent a document to GPT whenever DistilBERT's confidence was below one
threshold, whatever the label. Chosen on validation the same way: the most confident
mistake was 0.983, so 0.983 + 0.005 = **0.988** (a 0.02 margin would have given 1.0,
everything to GPT). DistilBERT's softmax confidence is compressed: correct documents
scored 0.97-0.992 (median 0.981), and the mistakes sat inside that band. 78% of
validation documents fell below 0.988.

On the holdout it scored 97.5%, sending 87.5% of documents to GPT (only invoices reached
0.988), at 944 ms per document and $0.27 per 1000 documents. Its one mistake was GPT's: a
payment reminder that GPT called a remittance, which DistilBERT had right at p=0.987.
Confidence did not separate DistilBERT's mistakes from its correct answers, so routing
by confidence alone saved little. Results:
`ml/results/classifier-hybrid-holdout-confidence-0.988.json`,
`ml/results/classifier-threshold-validation-confidence-0.988.json`.

## Consequences
- Accepted DistilBERT labels can never be `purchase_order`, so the hybrid adds no false-PO
  risk over GPT. A PO that DistilBERT mislabels as a non-PO with confidence above the
  threshold is accepted without GPT. That mistake is visible (a missing order), not a wrong
  order, and with v3 it did not occur on validation or holdout.
- Calibrating DistilBERT's confidence (e.g. temperature scaling on validation) would let a
  real non-PO threshold filter something; with v3 there is nothing to filter.
- A new champion needs a new threshold from `ml/choose_threshold.py`;
  `CLASSIFIER_NON_PO_THRESHOLD` changes without a code change.
- The classifier container needs about 600 MB of memory.

### Note for E14 (AWS deployment)
Locally the classifier image is 1.63 GB and downloads the ~540 MB model from MLflow at
every start, so start-up depends on MLflow and takes longer. On AWS the model will be
baked into the image at build time instead: the build resolves
`models:/o2c-doc-classifier@champion` and copies the artifacts into the image. A model
change is then a new image tag, deployed through GitOps (o2c-gitops values per
environment) like any code change. Rollback is a tag change, and pods start without
MLflow.
