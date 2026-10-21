# E10 Execution Guide — Models, Training Pipelines and MLflow

E10 was done in **fast mode**: Claude Code implemented each part from a written spec on one branch (`feat/e10-models`), with one PR at the end. This guide records the specs, the results, the decisions and every problem found. All work ran locally; no AWS was used. OpenAI cost: a few cents for evaluations.

## Results

| Area | Result |
| --- | --- |
| Extraction, golden set (50 POs) | 50 / 50 perfect, 100% field accuracy |
| Extraction, held-out set (20 harder POs) | 9 / 20 perfect, 99.0% field accuracy; all errors in `customer_number` |
| Document classifier, held-out (40 docs) | **DistilBERT 100.0%, 147.8 ms/doc, $0** vs GPT 97.5%, 1,191.7 ms/doc, $0.30 per 1000 docs |
| Quantity anomaly model v3 (`@champion`) | Precision 93.9%, recall 90.2%, F1 0.92; POs with a false alarm 2.9% (held-out 0%) |
| Kubeflow pipeline | Runs locally (kfp.local) and on Kubeflow Pipelines in kind; promotion rule kept v3 as champion |

## Part A — Datasets, shared scoring, baselines

- **Training set** `ml/data/train`: 100 POs (seed 2026), same six layouts as the golden set.
- **Held-out set** `ml/data/holdout`: 20 harder POs (seed 4242): two unseen layouts, two-page POs with repeated table headers, noise text (terms, bank footer, page x of y). Used only for final measurement, never for tuning.
- **Classifier data** `ml/data/classifier/{train,holdout}` with `labels.csv`.
- `poc/scoring.py` became `o2c_common.scoring`; `make_extractor` got a `model` parameter.
- `ml/eval_extraction.py` evaluates the **production extraction code**; `ml/eval_classifier_gpt.py` evaluates the production GPT classifier.
- Generators gained `--seed --count --out --hard`; without options they reproduce `poc/data` exactly (verified with `git diff`).

**Finding (ADR-005):** OpenAI closed self-serve fine-tuning to organisations that never ran it (7 May 2026) and ends all new jobs on 6 Jan 2027; `gpt-4.1-mini` shuts down on 14 Jan 2027. Decision: fine-tuning is demonstrated by fine-tuning DistilBERT locally with PyTorch; extraction stays prompt-based behind the LiteLLM alias `o2c-extract`, which moves to a current model in E11, guarded by the evaluation gate.

## Part B — customer\_number diagnosis and PyTorch classifier

**customer\_number:** the "formular" layout never prints the customer's number but prints Alpentech's supplier number at the customer; GPT returned that look-alike. A prompt rule fixed that layout but broke the "portal" layout (*Account no. at supplier*): held-out stayed at 9/20. Decision (Part C): revert the prompt rule and **verify in code** — the agent accepts an extracted customer number only if the ERP customer with that number exists **and** its name matches the PO; otherwise it resolves by name. A wrong number can no longer produce a wrong order.

**Classifier:** `ml/train_classifier.py` fine-tunes `distilbert-base-multilingual-cased` on CPU with a plain PyTorch loop (max\_length 256, batch 16, AdamW 5e-5, 3 epochs), logs params, per-epoch metrics, holdout metrics and the confusion matrix to MLflow (experiment `doc-classifier`), and registers `o2c-doc-classifier`.

- First run: validation 95.8%, held-out **47.5%** — it learned the training layouts, not the task (20 of 21 mistakes predicted as purchase orders).
- Retry (Part C): about 60 more training documents per type with the same *kinds* of variety as the hard set (footers, page numbers, payment reminders, confusing confirmations), generated with **training seeds only**. Held-out measured once: **100.0%**.

**Decision:** DistilBERT meets the rule (within 2 points of GPT) and is 8× faster at zero cost. Caveat: 40 synthetic documents is a small test. Plan for E11: use DistilBERT as the primary classifier with GPT as fallback below a confidence threshold, guarded by the evaluation gate.

## Part C / C2 — IsolationForest anomaly model

- `ml/train_anomaly.py` reads COMPLETED order lines from the ERP database. Features: log quantity, quantity / customer-material median, robust z-score, never-bought flag. IsolationForest with fixed seed; score 0–1; cut-off chosen for best F1 at the graph's 0.8 threshold.
- Packaged with the per customer-material statistics as an `mlflow.pyfunc` model, registered as `o2c-quantity-anomaly`, alias `@champion`.
- `agent/anomaly.py` loads `models:/o2c-quantity-anomaly@champion` once; if MLflow or the model is unavailable it logs a warning and returns 0.0. Compose sets `MLFLOW_TRACKING_URI=http://mlflow:5000` for the agent.

**Finding (ADR-006):** v2 scored F1 0.91 on the history but flagged **92.9%** of normal POs, because the ERP history (E5) and the POs (E4) came from unrelated generators. Fix: `poc/derive_customer_profiles.py` derives buying profiles from the generated POs into `poc/data/customer_profiles.json`; `seed_history.py` samples history from them (with `--reset` for generated `hist-` orders only), \~2% labelled outliers. New metric: **false-alarm rate on normal POs** (poc/data and held-out). v3: 2.9% of POs (held-out 0%), recall 90.2%. Limits: "never ordered" outliers are barely detected (1 of 6) because generated customers have little material preference; precision/recall are in-sample; held-out is the clean measure.

`local/anomaly_demo.py`: PO-0001 as received → posted; with line 1 ×20 → `QUANTITY_ANOMALY` (score 0.92) only.

## Part D — Kubeflow pipeline and promotion

- `ml/pipelines/anomaly_pipeline.py` (KFP SDK v2): load\_history → train → evaluate → register\_and\_promote, reusing refactored functions from `ml/train_anomaly.py`. Components run in one image (`docker/ml.Dockerfile`, `o2c-ml:dev`).
- **Promotion rule:** move `@champion` only if the PO-level false-alarm rate is lower and recall ≥ 80%. Unit-tested (better / worse / recall too low).
- Local run with `kfp.local` DockerRunner; compiled to `ml/pipelines/anomaly_pipeline.yaml`.
- Kubeflow Pipelines standalone installed on the kind cluster; UI via port-forward on 18088; `make kfp-run` uploads and runs the pipeline. Removal steps in `ml/pipelines/README.md` free the memory again.
- Verified: after the pipeline runs, `@champion` is still **version 3** — the new version was not better, so it was not promoted.
- The agent loads the champion at start: `docker compose restart agent` (later `kubectl rollout restart`) picks up a new one.
- ADR-007 records why a pipeline, local vs cluster runs, the promotion rule, and that heavy training stays local (AWS limits).

**Makefile targets added:** `ml-image`, `ml-pipeline-local`, `ml-pipeline-compile`, `kfp-install`, `kfp-ui`, `kfp-run`.

## Troubleshooting — problems hit during E10 and their fixes

| # | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| 1 | OpenAI fine-tuning not possible | OpenAI closed self-serve fine-tuning to new organisations (May 2026) | Fine-tune DistilBERT locally with PyTorch; ADR-005 |
| 2 | `RequestsDependencyWarning: Unable to find acceptable character detection dependency` | Optional helper package missing for `requests` | Harmless warning |
| 3 | Held-out 9/20, all `customer_number` | Look-alike supplier number on an unseen layout | Prompt rule only moved errors; final fix is a name check in the agent's `resolve_customer` |
| 4 | DistilBERT 47.5% on held-out despite 95.8% validation | Training data lacked the variety of real documents (distribution shift) | More varied training documents from training seeds → 100% |
| 5 | `make sync`: `Failed to uninstall ... hf_xet ... missing RECORD file` | A package had been installed with pip outside uv | Deleted leftovers, `make sync`; rule: only `uv add` |
| 6 | gitleaks blocked the commit: `generic-api-key` in `tests/conftest.py` | Hard-coded long test JWT secret | `JWT_SECRET = secrets.token_hex(32)` generated per test run |
| 7 | `InsecureKeyLengthWarning` in agent tests | Test secret shorter than 32 bytes | Random 32-byte secret (same fix as #6) |
| 8 | Full `mlflow` package could not be installed | It requires gunicorn < 24; intake needs gunicorn ≥ 26.2 (one lockfile) | `mlflow-skinny==2.17.2` (client only) |
| 9 | MLflow model logging failed | MLflow 2.17 adds torchvision to PyTorch model requirements | Explicit pip requirements when logging |
| 10 | MLflow transformers support broken with transformers 5.x | Version incompatibility | Pin `transformers<4.47`, `pandas<3` |
| 11 | Anomaly model flagged 92.9% of normal POs | History and POs generated independently | Profiles derived from POs; history regenerated; false-alarm metric added |
| 12 | Results asked for were not noted | Outputs scrolled away | Results are saved under `ml/results/` and in MLflow; future requests ask explicitly to note values |

**Lessons:**

- A held-out set is the only honest measure; never tune on it.
- Verify in code what code can verify; do not chase errors with prompt wording.
- Measure the business effect (false alarms on normal orders), not only model metrics.
- Synthetic data must be consistent across generators, or models learn the wrong thing.
- Promote models only by an explicit, tested rule.
