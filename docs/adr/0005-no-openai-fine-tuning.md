# ADR-005: No OpenAI fine-tuning; fine-tune a local transformer instead

Date: 2026-10-21
Status: Accepted

## Context
ADR-002 deferred fine-tuning to Phase 3 (E10). The plan was to fine-tune the
extraction model behind the LiteLLM alias `o2c-extract` (today `openai/gpt-4.1-mini`)
on our synthetic POs. The provider has since closed that path:

- OpenAI closed self-serve fine-tuning to organisations that had never run a
  fine-tuning job (deprecations page, 7 May 2026). This project's organisation never has.
- OpenAI ends all new fine-tuning jobs on 6 Jan 2027.
- gpt-4.1-mini, the model behind `o2c-extract`, shuts down on 14 Jan 2027.

A fine-tuned OpenAI model is therefore not available to us, and even where it is
available it would be a dead end within months.

## Decision
- Fine-tuning is demonstrated on a different task: document classification
  (purchase_order / invoice / remittance / other). A pretrained transformer
  (DistilBERT) is fine-tuned locally with PyTorch on `ml/data/classifier/train`
  (240 documents) and compared with the LLM classifier on `ml/data/classifier/holdout`
  (40 documents, unseen layouts and deliberately confusing documents).
- Extraction stays prompt-based behind the LiteLLM alias `o2c-extract`. In E11 the alias
  moves from gpt-4.1-mini to a current model. The move is guarded by an evaluation gate:
  the new model must score at least as well as the current one on `ml/data/holdout`
  (`ml/eval_extraction.py`, primary metric: perfect POs) before the alias changes.
- Services never name a provider model directly, so the switch is a LiteLLM
  configuration change, not a code change.

## Consequences
- The fine-tuning skill is shown end to end (data, training, evaluation, comparison
  with an LLM baseline) without depending on a provider's fine-tuning API.
- A local classifier runs without per-call cost or network, and can replace or pre-filter
  the LLM classifier if it is as accurate on the holdout set.
- Extraction quality depends on prompts and the chosen model. The holdout set (two
  layouts never seen in training, two-page orders, noise text) is what protects us
  from regressions when the model behind `o2c-extract` changes.
- The gpt-4.1-mini shutdown on 14 Jan 2027 is a hard deadline for E11.
