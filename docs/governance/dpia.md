# Data protection impact assessment (GDPR Art. 35)

> **Self-assessment for a portfolio project, not legal advice.** All purchase orders,
> contracts and customers in this repository are synthetic, so no real personal data is
> processed today. The assessment describes the system as if it processed real customer
> POs. A real deployment needs the controller's DPO.

Checked: 2026-10-28. Owner: Abhinav. Review: before any real data is processed, and
after any change to a store, a processor or an LLM call site.

## 1. Processing
- **Purpose.** Purchase orders that business customers email as PDFs are turned into ERP
  sales orders. Exceptions go to a human reviewer. The decisions are logged for audit
  (ADR-003, ADR-004, ADR-014).
- **Controller.** The selling company (here: the fictional supplier Alpentech).
- **Data subjects:**
  - contact persons of business customers: whoever sends the PO, and whoever is named,
    signs or is a delivery contact on it;
  - the controller's own staff: reviewers and release approvers.

### Is a DPIA mandatory?
Measured against the nine criteria of the WP29/EDPB DPIA guidelines (WP248 rev.01,
endorsed by the EDPB on 25 May 2018, [EDPB][edpb-dpia]), only one is met: a new
technology (LLMs).
- No evaluation or scoring of persons.
- No automated decision with legal or similar effect on a person.
- No systematic monitoring, no special categories, no vulnerable data subjects.
- Not large scale.

So a DPIA is **probably not mandatory**; it is done voluntarily, because LLM processing
by a third-country provider is the kind of new processing a supervisory authority asks
about. *Not verified:* the national DPIA lists of the supervisory authorities (Art. 35(4)),
for example Austria's or Germany's. They may name AI explicitly.

## 2. Personal data actually processed

| Data | Where it comes from | Where it goes |
|---|---|---|
| Sender email address | Intake request `sender` (`services/intake`) | Event `docs.received` payload; `raw.events` |
| Names, email, phone, signature of contact persons; company and delivery addresses | The PO PDF. The synthetic POs carry company names and addresses only; real POs carry contact persons. | The PDF in S3. Its text goes to OpenAI (extraction, classifier fallback) and into Langfuse traces. The extracted `customer_name` and `ship_to_address` go into events, `review_queue`, checkpoints, the audit archive and the ERP. |
| Reviewer username and full name; scrypt password hash | `local/users.json` (`agent.auth add-user`, git-ignored) | JWT (8 h), `review_decisions` (reviewer, reviewer_name, comment), audit archive, `analytics.mart_reviews` |
| Release approver and starter usernames; client IP addresses | Jenkins login | Jenkins audit trail (`audit-trail.log*`), `reports/approval.txt` |
| GitHub logins | GitHub | Promotion and rollback PRs (`make audit-check` lists who merged) |

**Not processed:**
- special categories (Art. 9);
- data about children;
- profiling or scoring of persons.

Prometheus labels exclude identifiers by rule and test (ADR-012). Grafana's business
datasource reads only `analytics.*`, as `grafana_ro` (ADR-013).

## 3. Legal basis
| Processing | Basis (Art. 6(1)) | Note |
|---|---|---|
| Customer contact data on POs, to fulfil B2B orders | (f) legitimate interest. (b) contract applies only to sole traders. | The contact persons expect their employer's orders to be processed. Minimal intrusion. |
| Keeping orders, decisions and the audit archive | (c) legal obligation (commercial and tax record-keeping) and (f) accountability | The period depends on national law (finance and legal to confirm) |
| Reviewer identity on every decision | (f) accountability for financial decisions, and (c) where internal-control rules require it. Employment law (Art. 88 and national law) applies. | Only for audit, never for performance evaluation (see [eu-ai-act.md](eu-ai-act.md) §4) |
| Approver identity (four-eyes) | (f) and (c), as above | Change-management evidence |
| Sending PO text to OpenAI | Same purpose as extraction; OpenAI is a processor under Art. 28 | Section 5 |

## 4. Minimisation, and retention per store

What already minimises data:
- **Text only to the LLM.** The extractor sends the PDF's text to the LLM, never the
  PDF. Invisible text is dropped (ADR-010).
- **Facts only for explanations.** The explainer sends the facts of the issues
  (`agent.explain.explanation_facts`), not the whole document.
- **Bounded fields.** The event schema has fixed fields. Contact persons are not
  extracted, because `PurchaseOrder` has no such field.
- **No identifiers in metrics.** Metric labels carry no identifiers (ADR-012).
- **Restricted BI.** The BI role cannot read `raw`, `staging` or `public` (ADR-013).

**Gaps:**
- The full PDF text, including contact persons, reaches Langfuse through LiteLLM's
  callback (`success_callback: ["langfuse"]` in `local/litellm/config.yaml`), and the
  Kafka payloads.
- Nothing is deleted automatically except Kafka and Prometheus data.

| Store | Holds | Retention today (local) | Target for real data |
|---|---|---|---|
| S3 `o2c-documents` (`incoming/<key>.pdf`) | Original PDFs | Indefinite, no lifecycle rule | Lifecycle delete when the order's retention ends. Short (e.g. 90 days) for non-PO documents. |
| Postgres `o2c.public`: `review_queue`, checkpoints, `processed_events` | PO data and agent state | Indefinite | Delete checkpoints of finished orders after N days. Keep `review_queue` like the order. |
| Postgres `o2c.public.review_decisions` | Reviewer, comment, PO before and after | Indefinite, append-only by rule (CLAUDE.md) | Legal retention, then delete by a documented exception to the append-only rule |
| Postgres `o2c.raw` (`events`, `dlq`, `llm_spend`) and `analytics` | Every event payload, unchanged | Indefinite; grows one row per event (ADR-013) | Partition by month and drop after the retention. Marts keep only aggregates. |
| Postgres `erp` | Customers, orders | Indefinite (ERP mock) | The ERP's own policy |
| Postgres `litellm` | Spend logs: request metadata, key alias, session id. Prompt storage is not enabled in the config. | Indefinite | e.g. 90 days |
| Kafka | Events | 7 days; 30 for `orders.exceptions`, `orders.posted` and `orders.dlq` (`o2c_common.topics.TOPICS`). Locally Kafka has no volume, so `make down` loses it. | Unchanged |
| Langfuse (ClickHouse, S3 `langfuse`) | Full prompts and completions, i.e. PDF text | **Indefinite.** Self-hosted retention needs the Enterprise Edition: minimum 3 days, nothing deleted by default ([Langfuse docs][lf-retention]). | Turn off message logging to Langfuse for production keys (`turn_off_message_logging`, [LiteLLM docs][litellm-logging]); keep message content only for evaluations on synthetic data |
| S3 `o2c-audit` (hash chain) | Decisions incl. customer name, amounts, reviewer | Indefinite locally. On AWS: Object Lock GOVERNANCE, 1 day (demo); COMPLIANCE with the legal period in production (ADR-014). | Legal retention period |
| Prometheus | Counters without identifiers | 15 days (`--storage.tsdb.retention.time=15d`) | Unchanged |
| Jenkins volume `o2c-jenkins-home` | Audit trail with usernames and client IPs; build reports | Rotated by the audit-trail plugin. Count and size not verified. | Rotation plus archive with the change records |
| MLflow (Postgres `mlflow`, S3 `mlflow`) | Models trained on synthetic data | Indefinite | No personal data, as long as training data stays synthetic or anonymised |

## 5. Processors and transfers
| Processor | Data | Basis | Facts (checked 2026-10-28) |
|---|---|---|---|
| **OpenAI** (via the self-hosted LiteLLM gateway) | PDF text; issue facts for explanations; contract text for embeddings | Art. 28 DPA | See below |
| GitHub | Code, PR metadata, images in ghcr.io (no customer data) | GitHub terms | Images and models contain no customer data |
| Sigstore (Fulcio, Rekor) | Signing certificates for the release workflow identity | Public transparency log | No personal data: the identity is the workflow, not a person (ADR-016) |
| Self-hosted, no processor: LiteLLM, Langfuse, MLflow, Postgres, Kafka, SeaweedFS, Prometheus, Grafana, Jenkins | | | Run on the controller's machine (later its AWS account) |
| Planned, not built: HubSpot and Zapier (E12), ServiceNow (E13) | | | Reassess this DPIA when they are added |

OpenAI API facts ([OpenAI, "Your data"][oai-data]):
- **Training.** Data sent to the API is not used to train or improve OpenAI models unless
  the customer opts in.
- **Retention.** Abuse-monitoring logs are kept for up to 30 days by default.
- **Zero Data Retention or Modified Abuse Monitoring.** Available per organisation or
  project, *subject to prior approval by OpenAI*.
- **Data residency.**
  - Set per project and used through a regional domain such as `eu.api.openai.com`.
  - In-region processing (inference) is available in the US, EU and UAE; storage in more
    regions.
  - Non-US residency requires approval for MAM or ZDR and a Modified Retention amendment.
  - Eligible models released on or after 5 Mar 2026 cost 10% more on residency
    endpoints.
  - System data (account, billing) is not covered.
  - OpenAI's 2025 announcement of EU data residency: [OpenAI][oai-eu].
- **DPA.** OpenAI offers a Data Processing Addendum for API customers
  ([openai.com/policies/data-processing-addendum][oai-dpa]). Transfers rely on Standard
  Contractual Clauses or adequacy decisions. *Not verified directly:* the English DPA
  page returned HTTP 403 to my fetcher. The SCC statement comes from OpenAI's translated
  DPA pages and secondary summaries.

**This project today:**
- The standard OpenAI API through one project key.
- No ZDR, no EU residency.
- Synthetic data only.

**For real data:**
1. Sign the DPA.
2. Apply for ZDR (or MAM) and an EU-residency project. Point LiteLLM's `api_base` at
   `https://eu.api.openai.com/v1` for every alias; it is one configuration change
   (ADR-005).
3. Record the transfer mechanism in the record of processing.

## 6. Data subject rights
| Right | How, today | Gap |
|---|---|---|
| Information (Art. 13/14) | Not built: a supplier's privacy notice would cover order processing | Add LLM processing and OpenAI as a recipient to the notice |
| Access (Art. 15) | Search by customer name or email: `review_queue`, `raw.events`, the ERP, Langfuse search; the PDF via the idempotency key in S3 | No tool. A script that collects every record of one customer or sender is needed for production. |
| Rectification (Art. 16) | Reviewer edit (recorded as a new decision); ERP master data | - |
| Erasure (Art. 17) | Limited while legal retention applies (17(3)(b), (e)). Langfuse traces, PDFs of non-orders and DLQ copies have no such reason and must be deletable. | No deletion tool. The hash chain makes erasure inside the archive impossible without breaking it. Option: crypto-shredding (per-customer key) or pseudonymised archive records. |
| Objection (Art. 21) | Legitimate-interest processing; a contact person can ask to be removed from future POs | Manual |
| Automated decisions (Art. 22) | Not applicable: decisions concern orders between companies, every exception is decided by a person, and posting rules are code | - |

## 7. Risks and mitigations
Likelihood and impact on a 1-3 scale for the data subjects (the project's own risks are in
[risk-register.md](risk-register.md)).

| # | Risk to data subjects | L | I | Mitigation in place | Residual / to do |
|---|---|---|---|---|---|
| D1 | PO text including contact data goes to a US LLM provider | 3 | 2 | Processor DPA available; no training on API data; text only, not the PDF | **Medium** until EU residency and ZDR are approved and configured |
| D2 | Full prompts kept indefinitely in Langfuse | 3 | 2 | Self-hosted, not public; admin login | **Medium**: turn off message logging for service keys, or set retention (EE) |
| D3 | Raw PDFs, events and checkpoints kept beyond need | 3 | 1 | Access limited to the stack; BI cannot read raw | **Medium**: lifecycle and partition retention |
| D4 | Reviewer audit data used to rate staff | 1 | 2 | Purpose limited to audit (this DPIA, [eu-ai-act.md](eu-ai-act.md)); `mart_reviews` per decision | Low; works council or HR agreement in a real company |
| D5 | Breach of a store (S3, Postgres) | 1 | 2 | Secrets only in `.env` (git-ignored); per-service keys; non-root signed images (Kyverno); `grafana_ro` read-only | Low locally; encryption at rest and IAM with the AWS module (not applied) |
| D6 | Erasure request cannot be met in the hash chain | 2 | 1 | Legal retention overrides for order records | Low; pseudonymise before archiving if needed |
| D7 | Prompt injection leaks data from one PO into another order | 1 | 2 | No cross-document context in prompts; injection detector; code checks (ADR-010) | Low |
| D8 | Personal data in metrics or logs | 1 | 1 | Label rule, tested (ADR-012); `check_env.py` never prints values | Low |

## 8. Conclusion
At synthetic-data scale the residual risk is low. **Before real POs are processed**,
complete these items:
- **D1:** DPA signed, ZDR and EU residency in place.
- **D2:** message logging off or retention set.
- **D3:** retention jobs per store.
- **Section 6:** an access and erasure tool.

Until then the system must not process real customer documents.

## Sources (checked 2026-10-28)
- Regulation (EU) 2016/679 (GDPR): [EUR-Lex][gdpr]
- EDPB-endorsed WP29 guidelines on DPIA (WP248 rev.01, endorsed 25 May 2018): [edpb-dpia]
- OpenAI API, "Your data" (training, retention, ZDR/MAM, residency): [oai-data]
- OpenAI, "Introducing data residency in Europe": [oai-eu]
- OpenAI Data Processing Addendum: [oai-dpa] (English page not fetched, HTTP 403)
- Langfuse, data retention: [lf-retention]
- LiteLLM proxy logging (message redaction): [litellm-logging]

[gdpr]: https://eur-lex.europa.eu/eli/reg/2016/679/oj
[edpb-dpia]: https://edpb.europa.eu/our-work-tools/our-documents/guidelines/data-protection-impact-assessments-high-risk-processing_en
[oai-data]: https://developers.openai.com/api/docs/guides/your-data
[oai-eu]: https://openai.com/index/introducing-data-residency-in-europe/
[oai-dpa]: https://openai.com/policies/data-processing-addendum/
[lf-retention]: https://langfuse.com/docs/administration/data-retention
[litellm-logging]: https://docs.litellm.ai/docs/proxy/logging
