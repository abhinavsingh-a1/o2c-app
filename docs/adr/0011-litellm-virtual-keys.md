# ADR-011: One LiteLLM virtual key per service, with a monthly budget

Date: 2026-10-22
Status: Accepted

## Context
Every service and every evaluation script called LiteLLM with `LITELLM_MASTER_KEY`. Spend
could not be attributed to a service, a bug that loops on LLM calls in one service could
spend without limit, and the master key (which can create and delete keys) was in every
container.

## Decision
- `ml/litellm_keys.py` creates one LiteLLM virtual key per caller through LiteLLM's admin
  API (with the master key), with `key_alias` = the caller and a monthly budget
  (`max_budget` in USD, `budget_duration: 1mo`, reset on the 1st of the month):

  | Key alias | Budget / month | Used by |
  |---|---|---|
  | classifier | $1 | classifier service (GPT fallback of the hybrid classifier) |
  | extractor | $2 | extractor service |
  | agent | $1 | agent service (explanations, material matching, contract embeddings) |
  | eval | $3 | ml/eval_*.py, ml/judge_explanations.py, ml/eval_gate.py |

  The budgets are about 10x the expected local usage. The script is idempotent: an
  existing key (found by alias) is kept and its budget corrected if it differs; a new key
  is printed once, as `LITELLM_KEY_<SERVICE>=sk-...` for `.env` (LiteLLM stores only a
  hash). `.env.example` has `replace-me` placeholders.
- `docker-compose.yml` passes each service its own key as `LITELLM_MASTER_KEY`, because
  the services' settings field is still called `litellm_master_key`. The name is kept for
  now (renaming it touches every service, its tests and the Helm values); the value is the
  service's virtual key, not the master key. An empty `LITELLM_KEY_<SERVICE>` falls back
  to the master key, so `make up` works before the keys exist (they can only be created
  once LiteLLM runs). Intake makes no LLM calls and keeps the shared environment.
- Evaluation scripts call `litellm_common.use_eval_key()`: with `LITELLM_KEY_EVAL` set
  (and not a placeholder), it replaces the master key for that process.
- `ml/litellm_spend.py` prints spend, budget, share used and reset date per key.

## Consequences
- Spend is visible per service and capped: when a key's budget is used up, LiteLLM
  rejects its calls until the reset, and only that service is affected.
- The master key is only needed by LiteLLM itself, the admin scripts and as a fallback.
  In the cluster (E14) the service keys go into per-service Kubernetes secrets and the
  master key into LiteLLM's secret only.
- Rename `litellm_master_key` to `litellm_api_key` (and the variable to `LITELLM_API_KEY`)
  in a later change, together with the Helm values.
- Not done yet: restricting each key to the model aliases it needs (`models` on the key);
  the fallback `o2c-extract -> o2c-extract-previous` would have to be allowed as well.
- Verified locally: each container authenticates with its own alias (`/key/info`), and an
  evaluation run appears under `eval` in `ml/litellm_spend.py`.
