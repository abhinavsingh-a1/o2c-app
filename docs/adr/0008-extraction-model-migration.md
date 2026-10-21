# ADR-008: Extraction model migration (gpt-4.1-mini shutdown)

Date: 2026-10-21
Status: Accepted. No candidate passed; `o2c-extract` stays on gpt-4.1-mini for now.

## Context
`o2c-extract` (extraction, classification fallback, agent helpers) points at
`openai/gpt-4.1-mini`, which shuts down on 14 Jan 2027 (ADR-005). On 14 Oct 2026 OpenAI
added the snapshot gpt-4.1-mini-2025-04-14 to its deprecations page with the same date and
named gpt-6-luna as the replacement.

## Candidates
`ml/list_models.py` lists the account's models (OpenAI models endpoint, real key from
`.env`; listing is not an LLM call) and the shutdown dates copied from the deprecations
page (checked 2026-10-21). Small/mini general-purpose chat models:

| Model | Shutdown |
|---|---|
| gpt-4.1-mini (current), gpt-4o-mini | 14 Jan 2027 |
| gpt-4.1-nano, o3-mini, o4-mini | 23 Oct 2026 |
| gpt-5-mini, gpt-5-nano | 11 Dec 2026 |
| gpt-5.4-nano | 1 Apr 2027 |
| gpt-5.4-mini, gpt-5.6-luna, gpt-5.6-terra, gpt-6-luna | none scheduled |

Chosen (no shutdown before mid-2027):
- **gpt-6-luna** → `o2c-extract-candidate`: OpenAI's named replacement for gpt-4.1-mini,
  newest family, $0.10 / $0.50 per 1M tokens (gpt-4.1-mini: $0.40 / $1.60).
- **gpt-5.6-terra** → `o2c-extract-candidate-2`: the former mini tier (replacement for
  gpt-5-mini), $2 / $12 per 1M tokens. A quality reference, likely too expensive.

Both are reasoning models with default effort (medium); no model-specific settings.

## LiteLLM configuration
- `drop_params: true`: reasoning models reject `temperature`, which every service sends.
  LiteLLM now drops unsupported parameters instead of failing the call.
- Fallback: if `o2c-extract` fails after LiteLLM's retries, the call is retried once on
  `o2c-extract-candidate`. Evaluations record fallbacks (`x-litellm-attempted-fallbacks`);
  there were none in the runs below.

## Evaluation
`ml/eval_extraction.py` now records latency per PO and cost per PO (LiteLLM's
`x-litellm-response-cost` header, read with an httpx response hook on the extractor's
client). `ml/compare_extraction.py` prints the table and applies the rule. Each model was
run once per dataset; holdout measured once.

| Model | Dataset | Perfect POs | Field accuracy | Avg latency | Cost / PO |
|---|---|---|---|---|---|
| gpt-4.1-mini (current) | poc/data (50) | 50/50 | 100.00% | 3.6 s | $0.00118 |
| | train, first 20 | 19/20 | 99.84% | 3.0 s | $0.00105 |
| | holdout (20) | 9/20 | 99.01% | 4.7 s | $0.00177 |
| gpt-6-luna | poc/data | **49/50** | 99.95% | 5.8 s | $0.00041 |
| | train, first 20 | 20/20 | 100.00% | 5.0 s | $0.00036 |
| | holdout | 10/20 | 99.10% | 8.0 s | $0.00062 |
| gpt-5.6-terra | poc/data | 50/50 | 100.00% | 2.8 s | **$0.00648** |
| | train, first 20 | 20/20 | 100.00% | 2.5 s | **$0.00570** |
| | holdout | 9/20 | 99.01% | 3.7 s | **$0.01002** |

Remaining errors: holdout `customer_number` on the "formular" layout for every model
(10-11, see ADR-005; the agent verifies the number against the ERP); one
`line.description` each for gpt-4.1-mini (train) and gpt-6-luna (poc/data, PO-0030:
"Rillenkugellager 6205-2RS C3 (your item no. 400104)" instead of
"Rillenkugellager 6205-2RS C3").

## Rule
A candidate replaces the current model only if on **every** dataset: perfect POs >= the
current model's, field accuracy at most 0.2 points lower, cost per PO at most 50% higher
(`compare_extraction.should_migrate`, unit-tested).

## Decision
- **gpt-6-luna fails** on poc/data: 49 < 50 perfect POs. It is better on train and holdout
  and 65% cheaper, but slower (about 1.7x). The single error is an extra
  "(your item no. ...)" in a description, which the agent's material matching tolerates,
  but the rule counts perfect POs and is applied as written. The run is not repeated to
  look for a pass.
- **gpt-5.6-terra fails** on cost: +443% to +466% per PO on every dataset, with no
  accuracy gain over the current model on the holdout.
- **`o2c-extract` stays on gpt-4.1-mini.** Both candidates stay configured; the fallback
  `o2c-extract -> o2c-extract-candidate` (gpt-6-luna) stays, so an outage of gpt-4.1-mini
  is covered by the cheaper model that came closest.

## Consequences
- The 14 Jan 2027 deadline is unchanged; a passing model is still needed before then.
  Options for the next round, each measured on poc/data and train first (the holdout has
  now been used to compare these models, so it is the final check only):
  - gpt-6-luna with an explicit `reasoning_effort` (lower latency, possibly fewer
    additions to descriptions);
  - a prompt rule that copies descriptions verbatim, checked for regressions on
    gpt-4.1-mini as well;
  - deciding whether the rule should count a description difference that the agent
    tolerates as a failed PO.
- Moving the alias later is one line in `local/litellm/config.yaml` plus a LiteLLM restart;
  services do not change.
- `drop_params` also applies to gpt-4.1-mini calls, where it changes nothing.
