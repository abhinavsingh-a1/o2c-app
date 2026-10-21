# ADR-006: Quantity anomaly model

Date: 2026-10-21
Status: Accepted

## Context
The agent graph flags `QUANTITY_ANOMALY` when an order line looks unusual for the
customer, for example 20x the usual quantity or a material the customer has never
ordered. E10 replaced the placeholder `anomaly_score() = 0.0` with a model trained on
the ERP order history (vbak/vbap, status COMPLETED). The labelled outliers in that
history are written by `erp_mock.seed_history` to `poc/data/history_outliers.json`.

## Decision
- **Model.** scikit-learn IsolationForest (200 trees, fixed `random_state`), trained by
  `ml/train_anomaly.py`. Features per order line (`agent/anomaly_model.py`, shared by
  training and the agent):
  - log quantity
  - quantity / median quantity of this customer-material
  - robust z-score for this customer-material: 0.6745 x (quantity - median) / MAD, with
    the MAD floored at 10% of the median so steady buyers do not divide by ~0
  - "never ordered": no other purchase of this material by this customer (in training,
    the line does not count as a purchase of itself)
- **Score and threshold.** The IsolationForest output goes through a sigmoid, giving a
  score from 0 (normal) to 1 (very unusual). The sigmoid is centred so that the graph's
  fixed threshold `score > 0.8` falls exactly on the cut-off with the best F1 on the
  labelled history outliers. The threshold in the graph never changes; retraining moves
  the calibration instead.
- **Packaging.** Forest, per customer-material statistics and calibration are one
  `mlflow.pyfunc` model in experiment "quantity-anomaly", registered as
  `o2c-quantity-anomaly`. The agent loads `models:/o2c-quantity-anomaly@champion`.
  Loading needs the agent package, because the model class lives in `agent.anomaly_model`.
- **Promotion.** A new version is registered on every training run. `@champion` moves only
  if its false-alarm rate (below) is lower than the current champion's.
- **False-alarm rate.** This is the metric that matters for touchless processing: the share
  of *normal* PO lines, and of POs, scored above 0.8. It is measured on `poc/data`
  (50 POs) and `ml/data/holdout` (20 POs); every line there is normal. Targets: under 5% of
  POs with at least one flag, and recall of at least 80% on the labelled history outliers.
- **Fallback.** If MLflow is down or the model is missing, `agent.anomaly` logs a warning
  and returns 0.0, so the agent never fails because of the anomaly model. It retries the
  load at most every 5 minutes. Docker Compose sets `MLFLOW_TRACKING_URI=http://mlflow:5000`
  for the agent.
- **New champion.** The agent keeps the loaded model in memory. After `@champion` moves,
  `docker compose restart agent` (later on Kubernetes: `kubectl rollout restart`) makes it
  load the new version. Training runs as a Kubeflow pipeline (ADR-007).

## Findings

### Data consistency
The first model (v1/v2) flagged `QUANTITY_ANOMALY` on 25 of the first 26 golden POs. The
model was not the problem; the data was. The ERP history had been generated
independently of the PO generators. Each history customer bought a random fixed set of
6-10 materials, while the generated POs order any of the 25 materials in any quantity of
the unit's range. So a normal golden PO looked unusual twice over: mostly "never ordered"
materials, and quantities unlike the history.

Fix:
- `poc/derive_customer_profiles.py` derives buying profiles from the 150 generated POs
  (poc/data + ml/data/train): each customer's materials with their order frequency, and
  every quantity ordered per material. Quantities are pooled across customers, because
  the PO generator draws them per unit, not per customer, and per customer-material
  there are only 2-5 lines. The result is committed as `poc/data/customer_profiles.json`.
- `erp_mock.seed_history` samples 12 months of history from these profiles: materials by
  the customer's order frequency, quantities bootstrapped from the real ordered ones.
  About 2% of lines are labelled outliers: 5-20x the material's median, or a material the
  customer never orders. `--reset` deletes only the generated history (idempotency key
  `hist-...`) before seeding again.

A first version of the new history used each never-ordered pair repeatedly and gave those
lines random quantities of 1-20. The repeats are not "never ordered" any more after the
first time, and the small quantities polluted the statistics: the model missed all 10
of these labels and raised 2 false alarms on the holdout. Both problems are visible from the
history alone. The holdout flags were inspected only to find the cause. Now each
never-ordered pair is used once, with a normal quantity for that material.

### Before / after

| Model | History | Precision | Recall | F1 | False alarms: POs (poc / holdout / combined) | False alarms: lines (combined) |
|---|---|---|---|---|---|---|
| v2, independent history (champion before) | 1,890 lines, 27 outliers | 89.3% | 92.6% | 0.909 | 90.0% / 100.0% / **92.9%** | 53.9% (144 never ordered, 70 quantity) |
| profile history, first version (not registered) | 2,228 lines, 52 outliers | 93.3% | 80.8% | 0.866 | 2.0% / 10.0% / 4.3% | 0.8% |
| **v3, profile history (champion now)** | 2,209 lines, 51 outliers | 93.9% | 90.2% | 0.920 | 4.0% / 0.0% / **2.9%** | 0.5% (all quantity) |

- Both targets are met: 2.9% of POs flagged (target under 5%) and 90.2% recall (target 80%).
- `local/anomaly_demo.py` now finds a clean golden PO at the first try (PO-0001; before,
  it was PO-0026). With one line at 20x, that line scores 0.92 and gets `QUANTITY_ANOMALY`.

### Limits
- **Precision, recall and F1 are in-sample.** The cut-off is chosen on the same labelled
  history lines it is measured on. The false-alarm rate is the out-of-sample check.
- **`poc/data` is not fully independent.** Its POs are among the 150 the profiles come
  from. The holdout (different seed, unseen layouts) is the clean measure.
- **Never-ordered materials are hard to detect.** Every quantity outlier is found
  (45/45), but only 1 of the 6 never-ordered outliers. The PO generator gives customers
  no real material preferences: 5 of 8 customers order all 25 materials, and the 6 gaps
  are sampling gaps. So "never ordered" is rare in the data and looks much like a normal
  rarely-ordered material. Real customer data with real buying patterns should make this
  feature stronger; until then, quantity is what the model reliably catches.
- The remaining 2 false alarms (poc/data) are normal quantities at the top of the
  generator's range for a customer-material with a low median.
