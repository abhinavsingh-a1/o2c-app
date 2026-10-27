# Model card: quantity anomaly

Part of the governance pack ([../eu-ai-act.md](../eu-ai-act.md)). Self-assessment for a
portfolio project, not legal advice. Last updated: 2026-10-28.

| | |
|---|---|
| Owner | Abhinav |
| Component | `agent.anomaly` (scoring), `agent.anomaly_model` (features, shared with training) |
| Model | MLflow `o2c-quantity-anomaly` **v3** `@champion`, an `mlflow.pyfunc` with scikit-learn IsolationForest (200 trees, fixed `random_state`), per customer-material statistics and the calibration. Baked into the agent image: `ghcr.io/abhinavsingh-a1/o2c-model-quantity-anomaly:v3@sha256:ac11abb6...` (`models.lock.json`, ADR-016). |
| Training | Kubeflow pipeline `load_history -> train -> evaluate -> register_and_promote` (`ml/pipelines/anomaly_pipeline.py`, ADR-007), the same functions as `ml/train_anomaly.py` |
| Output | Score 0 (normal) to 1. The graph raises `QUANTITY_ANOMALY` above the fixed threshold **0.8**, and the order goes to a person. |

## Features (per order line)
- log quantity;
- quantity / median quantity of this customer-material;
- robust z-score `0.6745 x (q - median) / MAD`, with the MAD floored at 10% of the median;
- "never ordered" (no other purchase of this material by this customer).

The IsolationForest output goes through a sigmoid. The sigmoid is centred so that 0.8 is
the best-F1 cut-off on the labelled history outliers.

## Intended use
- Stop unusual quantities (for example 20x the usual amount) for human review before
  posting.
- **It can only send an order to review. It can never post or reject one.**

## Data
- ERP order history (vbak/vbap, status COMPLETED): 2,209 lines with 51 labelled outliers
  (about 2%).
- Sampled by `erp_mock.seed_history` from buying profiles derived from the generated POs
  (`poc/data/customer_profiles.json`). Synthetic.

## Metrics (v3, ADR-006)
| Metric | Value | Target |
|---|---|---|
| Precision / recall / F1 on labelled history outliers (in-sample) | 93.9% / 90.2% / 0.920 | recall >= 80% |
| **False-alarm rate, POs** (poc/data / holdout / combined; every line is normal) | 4.0% / **0.0%** / **2.9%** | < 5% |
| False-alarm rate, lines (combined) | 0.5% | - |
| Quantity outliers found | 45 / 45 | - |
| "Never ordered" outliers found | 1 / 6 | - |
| Full gate 2026-10-28: normal line / 20x line / scoring errors | 0.076 / 0.983 / 0 | < 0.8 / > 0.8 / 0 |

**False alarms matter most:** each one costs a touchless order. v2 flagged 92.9% of
normal POs, because the history and the POs came from unrelated generators. The fix was
in the data, not the model (ADR-006).

## Promotion rule
Enforced in `train_anomaly.should_promote` and unit-tested (ADR-007):
- `@champion` moves only if recall is >= 80% **and** the PO false-alarm rate is lower
  than the champion's. A tie keeps the champion.
- The reason is stored as MLflow tags.

## Known limits
- Precision and recall are in-sample. The false-alarm rate on the holdout is the clean
  measure.
- Never-ordered materials are hard to detect (1 of 6): generated customers have no real
  preferences.
- The model knows only customer-material pairs from the history. A pair without history
  gets ratio 1 and z-score 0, and only the "never ordered" flag and the log quantity carry
  a signal (`anomaly_model.features`). New customers are therefore checked weakly for
  quantity.

## The pandas drift incident (2026-10-27, ADR-006)
1. Dependabot major bumps (#49 pandas 3, #50 transformers 5) were merged.
2. MLflow 2.17's schema enforcement then rejected every prediction (`Can not safely
   convert str to <U0`).
3. The agent's fallback **silently scored 0.0**, so quantity anomaly detection was off in
   dev and test. Red-team RT-08 (40x quantity) was posted.
4. The full eval in Jenkins promote-to-test caught it (red team 7/8); unit tests had not
   (the test model had no signature).

Fixes:
- `pandas>=2.2,<3` pinned, with a test that scores a *signed* pyfunc model;
- `o2c_anomaly_scoring_failures_total{reason}` and the critical alert
  `AnomalyScoringFailing`;
- the gate step "anomaly model scores", which has no fallback.

Rule: a fallback that hides a model must be counted, alerted on and gated.

## Monitoring
- `AnomalyScoringFailing` (critical, any failure in 10 minutes).
- `o2c_issues_total{code="QUANTITY_ANOMALY"}` and `TouchlessRateLow`.
- Gate step "anomaly model scores" in `--quick` and `--full`.

## Rollback
- Locally: move the MLflow alias `@champion` back to the previous version, then
  `docker compose restart agent`.
- Environments: a `models.lock.json` PR, then the release, then promotion; or revert the
  gitops promotion ([../../operations/runbooks.md](../../operations/runbooks.md)).
