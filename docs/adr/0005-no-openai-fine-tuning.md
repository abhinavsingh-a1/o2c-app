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

## Findings

### customer_number on the hard holdout (E10 Part B)
Model: `o2c-extract` (gpt-4.1-mini), layout-mode text, `ml/eval_extraction.py`.

- **Symptom.** `ml/data/holdout` scored 9/20 perfect POs, field accuracy 99.01%. All
  11 errors were `customer_number`, all in the "formular" layout.
- **Cause (not the generator).** "formular" never prints the customer's number at
  Alpentech, so the ground truth is null in all 11 POs, consistently. The form does print
  Alpentech's supplier number at the customer ("Ihre Lieferantennummer bei uns", 8 digits),
  and the model returned that look-alike every time.
- **Fix.** One general rule in `prompts/extract.md`: customer_number is the number under
  which Alpentech knows the customer; never the supplier/vendor number the customer gave
  Alpentech, a PO, requisition, cost-centre, tax or bank number; null if not printed.
- **No regression first** (before -> after the rule):

  | Dataset | Perfect POs | Field accuracy |
  |---|---|---|
  | poc/data (50) | 50 -> 50 | 100% -> 100% |
  | ml/data/train, first 20 | 19 -> 20 | 99.84% -> 100% |

  The single train error before the rule was a line description, unrelated to the rule;
  its disappearance is treated as run-to-run noise, not as an improvement.
- **Holdout, measured once after the rule:** 9/20 perfect, 99.01%. Unchanged in total,
  but the errors moved:

  | Layout | customer_number errors before | after |
  |---|---|---|
  | formular (11 POs, number not printed) | 11 | 2 (still the supplier number) |
  | portal (9 POs, printed as "Account no. at supplier") | 0 | 9 (now null) |

  The model now reads "supplier" in the portal label as the excluded supplier number.
- **Conclusion.** The rule trades one look-alike confusion for another: labels that say
  where a number lives ("at supplier", "bei uns") rather than whose number it is are the
  hard case. The holdout has now been used to judge this rule, so the wording is not tuned
  further against it. Further prompt work needs its own hard-layout validation set (new
  seed, separate from the holdout); the holdout stays the final gate for E11.
