# EU AI Act assessment

> **Self-assessment for a portfolio project, not legal advice.** The system runs on
> synthetic data on one developer machine. It was never placed on the EU market. A real
> deployment needs a review by counsel and the company's AI governance function.

Checked: 2026-10-28. Owner: Abhinav (o2c-app CODEOWNERS).

## 1. Legal status on the date checked

| Date | What applies | Source |
|---|---|---|
| 1 Aug 2024 | Regulation (EU) 2024/1689 (AI Act) enters into force | [EUR-Lex 2024/1689][aia], [Commission][ec-fw] |
| 2 Feb 2025 | Prohibited practices (Art. 5) and AI literacy (Art. 4) | [Commission][ec-fw] |
| 2 Aug 2025 | Governance rules and obligations of general-purpose AI (GPAI) model providers | [Commission][ec-fw], [Commission GPAI page][ec-gpai] |
| 19 Nov 2025 | Commission proposes the Digital Omnibus on AI | [Commission][ec-fw] |
| 7 May 2026 | Political agreement on the Omnibus | [Commission][ec-fw] |
| 8 Jul 2026 | Regulation (EU) 2026/1744 (Digital Omnibus on AI) adopted | [EUR-Lex 2026/1744][omnibus], [AI Act Explorer][aiact-omnibus] |
| 20 Jul 2026 | Commission guidelines on the Art. 50 transparency obligations (non-binding) | [Commission][ec-art50] |
| 27 Jul 2026 | Omnibus enters into force | [Commission][ec-fw] |
| 2 Aug 2026 | The Act applies generally, including Art. 50 transparency. The Commission's enforcement powers over GPAI providers apply. | [Commission][ec-fw], [Commission][ec-art50], [Commission GPAI page][ec-gpai] |
| 2 Dec 2026 | Art. 50(2) marking deadline for generative systems placed on the market before 2 Aug 2026 (Art. 111(4) as amended). New prohibition 9 (non-consensual intimate content and CSAM) | [AI Act Explorer][aiact-omnibus], [Commission][ec-fw] |
| 2 Dec 2027 | High-risk obligations for Annex III systems (was 2 Aug 2026) | [Commission][ec-fw], [AI Act Explorer][aiact-omnibus] |
| 2 Aug 2028 | High-risk obligations for Annex I systems (safety components of regulated products) | [Commission][ec-fw], [AI Act Explorer][aiact-omnibus] |

**Postponements.** The Omnibus that moved the high-risk dates is *adopted and in force*.
I found no further pending postponement on the date checked.

The Omnibus also amended Art. 4. Providers and deployers still have to "take measures
to support the development of AI literacy" of their staff, and the Commission and
Member States support them ([AI Act Explorer][aiact-omnibus]).

**What was not read directly.** The EUR-Lex page of 2026/1744 did not render for the
tool I used. The article texts quoted above come from the AI Act Explorer copy of the
Omnibus. The Official Journal is authoritative.

## 2. The system and its AI components
The inventory with versions is in [ai-register.md](ai-register.md). In short:

| Component | Technique | Decides |
|---|---|---|
| Extraction (`services/extractor`) | GPAI model through LiteLLM alias `o2c-extract` (gpt-6-luna, ADR-008) | Nothing. Turns a PDF's text into a structured PO. Code checks every value against the ERP. |
| Document classifier (`services/classifier`) | Fine-tuned DistilBERT + GPAI fallback (ADR-009) | Which documents go on to extraction. A PO label is always double-checked by the LLM. |
| Quantity anomaly model (`agent.anomaly`) | IsolationForest (ADR-006) | Raises the issue `QUANTITY_ANOMALY`, which sends an order to a human |
| Agent helpers (`agent.materials`, `agent.contracts`, `agent.explain`) | GPAI model | Proposes a material for a line, reads a contract tolerance, and writes the explanation a reviewer reads. A material must be in the ERP candidate list (`agent.materials`). |
| Agent graph (`agent.graph`) | Deterministic code (LangGraph) | Posts an order only with no issues and a total below the limit (ADR-004). Otherwise a human decides. |

The orders are B2B: buyer and seller are companies. The system makes **no decision about
a natural person**. The personal data it touches is listed in [dpia.md](dpia.md):
contact names and addresses on POs, and reviewer identities.

**Role.** The project develops these AI systems and puts them into service for its own
use. It is therefore the **provider** and also the **deployer** of each system (Art. 3(3)
and (4)). It **integrates GPAI models** (OpenAI, via LiteLLM). It is not a GPAI model
provider (section 5).

## 3. Prohibited practices (Art. 5): none
The prohibited practices are:
- harmful manipulation;
- exploiting vulnerabilities;
- social scoring;
- predicting crime from profiling;
- untargeted scraping of facial images;
- emotion recognition at work or in education;
- biometric categorisation by sensitive traits;
- real-time remote biometric identification for law enforcement;
- from 2 Dec 2026, generating non-consensual intimate content or CSAM.

None applies. The system:
- reads purchase orders;
- does not process biometric data;
- does not profile, score or influence people;
- generates only short business explanations for staff.

## 4. High-risk (Art. 6, Annex I and III): not high-risk

**Annex I (Art. 6(1)).** The system is not a product, or the safety component of a product,
covered by Union harmonisation legislation (machinery, medical devices, vehicles and so on).

**Annex III (Art. 6(2)).** Area by area:

| Annex III area | Applies? | Why |
|---|---|---|
| 1 Biometrics | No | No biometric data |
| 2 Critical infrastructure | No | Not a safety component of digital infrastructure, traffic or utilities |
| 3 Education | No | - |
| 4 Employment, workers management | No, with one boundary (below) | Reviewers are employees, but the system does not recruit, allocate tasks by personal traits, or evaluate or monitor them |
| 5 Essential services | No | No public benefits, no creditworthiness of natural persons, no life or health insurance pricing, no emergency calls. Customers are companies. |
| 6 Law enforcement, 7 Migration, 8 Justice and democracy | No | - |

**Conclusion: not high-risk.** The Art. 6(3) derogation is not needed, because no Annex
III use case applies in the first place. So there is no registration under Art. 49(2).

The 2 Dec 2027 date does not affect the classification. It would only matter if the
system crossed one of these lines:

- **Worker evaluation (Annex III 4(b)).** `analytics.mart_reviews` records reviewer,
  action and waiting time per decision (ADR-013). Using it to evaluate reviewers'
  performance, or to allocate their work by their behaviour, would be an Annex III use.
  It is reported per decision for audit only. Keep it that way, or reassess.
- **Creditworthiness (Annex III 5(b)).** Blocking or approving orders of sole traders
  (natural persons) by an AI credit or risk score would be a high-risk use.
- **Automated decisions with legal effect on persons.** Today a human reviews every
  exception, and posting rules are code, not model output (CLAUDE.md rule).

## 5. General-purpose AI models: deployer, not provider
- The GPAI models (gpt-6-luna for `o2c-extract`, gpt-4.1-mini as fallback, gpt-5.6-terra
  as evaluation judge, text-embedding-3-small) are OpenAI's. The GPAI provider obligations
  (Art. 53 to 55: documentation, copyright policy, training data summary) are OpenAI's.
  They applied from 2 Aug 2025 ([Commission GPAI page][ec-gpai]).
- **The project does not modify a GPAI model.** OpenAI fine-tuning was not used
  (ADR-005); the models are used through prompts only. Under the Commission's guidelines
  only "significant modifications" make a downstream modifier a provider
  ([Commission GPAI page][ec-gpai]). The guidelines' indicative thresholds come from
  secondary sources: a model is presumed GPAI above 10^23 FLOP of training compute, and a
  modification counts when it uses more than one third of the original model's compute
  ([WilmerHale, 24 Jul 2025][wh-gpai]). I could not open the guidelines document itself.
- **DistilBERT** (`distilbert-base-multilingual-cased`, fine-tuned locally, ADR-005) is
  a small encoder trained for 3 epochs on 480 documents on a CPU. That is many orders of
  magnitude below those thresholds. The result is a 4-class classifier, not a model that
  generates content. It is an AI system component, not a GPAI model.
- **What the deployer does with GPAI:**
  - Every call goes through LiteLLM (CLAUDE.md), with per-service keys and budgets
    (ADR-011).
  - Model changes go through the evaluation gate (ADR-008, ADR-010).
  - Outputs that reference master data are checked against the ERP.

## 6. Transparency (Art. 50, applies from 2 Aug 2026)

| Obligation | Assessment |
|---|---|
| 50(1) AI systems intended to interact directly with natural persons | **Not triggered.** No chatbot or interactive assistant. Reviewers use a work queue. Customers send PDFs by email and get no AI interaction. |
| 50(2) providers of systems generating synthetic text: machine-readable marking | **Low exposure, not fully resolved.** The extraction output is structured data copied from the PO, which arguably does not substantially alter the input (exception in 50(2)). The explanation is generated text, but it is shown only to internal reviewers and never published. *Not verified:* how the 20 Jul 2026 guidelines treat internal-only generated text; I did not read the guidelines document. |
| 50(3) emotion recognition / biometric categorisation | Not applicable |
| 50(4) deep fakes; text published to inform the public | Not applicable: nothing is published |

**Voluntary transparency measures.**
- The ADRs, this pack and the model cards document every AI component.
- The review UI heading reads "Why this order needs you". It does **not** yet label the
  explanation as AI-generated. Adding "AI-generated, check against the issues and the
  PDF" is a small open item (risk R-09 in [risk-register.md](risk-register.md)).

## 7. AI literacy (Art. 4)
Applies since 2 Feb 2025 and is amended by the Omnibus (section 1). What exists:
- The ADRs explain each model's limits.
- The model cards ([model-cards/](model-cards/)) and [human-oversight.md](human-oversight.md)
  tell a reviewer what the AI does and does not decide.

A real deployment adds reviewer onboarding on these documents, with a record of who
completed it.

## 8. Classification and reasoning

**Classification: not prohibited, not high-risk, Art. 50 not triggered (one point open,
section 6). Deployer of third-party GPAI models, not a GPAI provider. Art. 4 AI literacy
applies.** In the common shorthand: a minimal-risk system with limited transparency
exposure.

Reasoning:
1. No Art. 5 practice.
2. No Annex I product. No Annex III use case: the system processes B2B orders and makes
   no decision about natural persons.
3. Art. 50 targets systems that interact with people or publish synthetic content. This
   one does neither.
4. GPAI models are used unmodified through an API.

## 9. Voluntary measures already in place
The high-risk requirements (Art. 9 to 15) do not apply. The system still meets much of
their intent:

| Art. (intent) | Measure | Evidence |
|---|---|---|
| 9 Risk management | Risk register with controls and residual risk; red team | [risk-register.md](risk-register.md), ADR-010 |
| 10 Data governance | Separate train, validation and holdout sets; holdout never used for tuning (CLAUDE.md); synthetic data only | ADR-005, ADR-006, ADR-009 |
| 11 Technical documentation | 18 ADRs, model cards, this pack | `docs/adr/`, [model-cards/](model-cards/) |
| 12 Record-keeping | Hash-chained archive of every decision and override, `verify_audit.py`; Langfuse traces; Jenkins audit trail | ADR-014, `make audit-check` |
| 13 Transparency to deployers | Model cards with intended use and limits | [model-cards/](model-cards/) |
| 14 Human oversight | Code decides from explicit rules. Every issue and every order above 10,000 EUR goes to a person. Approve, edit and reject are recorded append-only. Four-eyes approval for prod releases. | [human-oversight.md](human-oversight.md), ADR-004, ADR-018 |
| 15 Accuracy, robustness, cybersecurity | Evaluation gate with thresholds (`make eval`); red team 8/8; injection detector; signed images; Kyverno admission | ADR-010, ADR-016, ADR-017, [threat-model.md](threat-model.md) |
| Post-market monitoring (72) | Prometheus alerts: touchless rate, anomaly-scoring fallback, LLM cost; promotion gate re-runs the eval | ADR-006, ADR-012, ADR-018 |

## Sources (all checked 2026-10-28)
- Regulation (EU) 2024/1689, EUR-Lex: [aia]
- Regulation (EU) 2026/1744 (Digital Omnibus on AI), EUR-Lex: [omnibus] (did not render
  for me; content checked via the AI Act Explorer copy, [aiact-omnibus])
- European Commission, "AI Act" regulatory framework page (timeline, Omnibus dates): [ec-fw]
- European Commission, Guidelines on Art. 50 transparency obligations (published
  20 Jul 2026): [ec-art50]
- European Commission, Guidelines for providers of GPAI models (application dates,
  downstream modifiers): [ec-gpai]
- Secondary: WilmerHale on the GPAI guidelines' compute criteria: [wh-gpai]

[aia]: https://eur-lex.europa.eu/eli/reg/2024/1689/oj
[omnibus]: https://eur-lex.europa.eu/eli/reg/2026/1744/oj
[aiact-omnibus]: https://artificialintelligenceact.eu/ai-act-explorer/digital-omnibus/
[ec-fw]: https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai
[ec-art50]: https://digital-strategy.ec.europa.eu/en/library/guidelines-transparency-obligations-providers-and-deployers-ai-systems
[ec-gpai]: https://digital-strategy.ec.europa.eu/en/policies/guidelines-gpai-providers
[wh-gpai]: https://www.wilmerhale.com/en/insights/blogs/wilmerhale-privacy-and-cybersecurity-law/20250724-european-commission-issues-guidelines-for-providers-of-general-purpose-ai-models
