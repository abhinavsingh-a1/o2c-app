# Human oversight

Self-assessment for a portfolio project, not legal advice. Last updated: 2026-10-28.

**Principle:** the agent never lets the LLM decide whether an order is posted. Code
decides from explicit rules, and a person decides everything those rules do not clear
(CLAUDE.md, ADR-004).

## 1. When a human decides

### Orders: the review queue
The graph posts an order automatically only if **all** of these hold (`agent.graph`,
decide node):
- no issue was raised;
- the order total is not above `auto_post_limit`: 10,000 EUR by default
  (`agent.settings.Settings.auto_post_limit`, env `AUTO_POST_LIMIT`).

Every other order pauses in `human_review` (LangGraph `interrupt()`) with status
`awaiting_review`. The issue codes that stop an order are:

| Issue | Raised by |
|---|---|
| UNKNOWN_CUSTOMER, CUSTOMER_MISMATCH | Customer resolution against the ERP (name must match the number) |
| UNKNOWN_MATERIAL | Material not in the ERP, or an LLM match not among the ERP candidates |
| NO_AGREED_PRICE, PRICE_DEVIATION | ERP price table plus contract tolerance (RAG reads the tolerance; code compares) |
| QUANTITY_ANOMALY | Anomaly model score > 0.8 |
| TOTAL_MISMATCH | Line totals or the order total do not add up |
| ABOVE_AUTO_POST_LIMIT | Total above the limit |
| PROMPT_INJECTION_SUSPECTED | Injection detector found instruction-like text (always review, also after an edit) |
| ORDER_VERIFY_FAILED | The ERP order differs from the PO after posting; the saga cancelled it |
| APPROVAL_NOT_POSSIBLE | A reviewer approved an order with an unknown customer or material |

**Not yet implemented:** a limit configurable per environment. The setting can be set per
environment through the Helm values, but no environment overrides the default today.

### Releases: four-eyes approval
promote-to-prod (o2c-infra `jenkins/Jenkinsfile.promote-prod`, ADR-018) waits at
**Approval (four eyes)**:
- The approver must be a different Jenkins user from the one who started the build. The
  build fails with "four-eyes rule: <user> started this build and cannot approve it"
  (proven in promote-to-prod #3).
- The approver role `approver` may only read, build and approve (JCasC matrix
  authorisation).
- Releases to test go through the eval, ZAP and k6 gates. They are scheduled by a person
  (Build Now), but need no second person.

### Models
- `@champion` moves only by the coded promotion rules (ADR-007; ADR-005 for the
  classifier).
- Production uses a model only after a person merges a PR that changes
  `models.lock.json`, and the release is promoted (ADR-016).

## 2. What the reviewer sees
The review UI (`ui/`, at http://localhost:5180 in Compose) shows, for the order
`GET /reviews/{key}` returns:

**Review queue:** PO number, customer, total, issue codes, status, last update.

**Detail page:**
- the **original PDF** next to the data (`GET /reviews/{key}/document`, from S3);
- **"Why this order needs you"**: the LLM explanation. It is checked by the judge in the
  gate. If it fails, the plain issue list is shown instead;
- **Issues**: each coded issue with its message, e.g. both prices and the tolerance with
  its contract evidence;
- **Order**: customer and resolved customer number, delivery date, ship-to, total and,
  once posted, the sales order number;
- **Lines**: material, description, quantity, unit, price and total;
- **Decision history**: every earlier decision with time, reviewer, action, result and
  comment (`GET /reviews/{key}/audit`).

What the reviewer does **not** see: the model scores (anomaly score, classifier
confidence). They are in the events and Langfuse.

**Gap (R-09):** the UI does not label the explanation as AI-generated.

## 3. Override
`POST /reviews/{key}` with `action`:

| Action | Effect |
|---|---|
| `approve` | Posts the order as extracted. Refused (`APPROVAL_NOT_POSSIBLE`) if the customer or a material is unknown, because there is nothing valid to post. |
| `edit` | Takes a corrected PO (the edit form computes the money exactly, with Decimals). **All checks run again.** If an issue remains, the order pauses again. |
| `reject` | Ends the order with status `rejected`. **A comment is required** (`test_reject_without_comment_returns_422`). |

Safeguards:
- A decision for an order that is not waiting returns 409
  (`test_second_review_returns_409`).
- A crash between decision and posting resumes from the Postgres checkpoint (thread id =
  idempotency key, `agent.checkpoint.run_or_resume`).

A reviewer can override every AI output:
- a wrong extraction, by editing;
- a false anomaly, by approving;
- a misleading explanation, because the issues and the PDF are shown with it.

A reviewer cannot override the coded rules: approving an unknown customer is refused, and
an edit is checked again.

## 4. Authentication and audit
- **Every `/reviews` endpoint requires a logged-in user** (`CurrentUser`; JWT 8 h,
  scrypt password hashes in `local/users.json`). Tests: `services/agent/tests/test_auth.py`.
- **The reviewer is always taken from the token**, never from the request body
  (`test_decision_records_logged_in_user_as_reviewer`).
- **`review_decisions` is append-only.** Each row records reviewer, name, action,
  comment, PO before and after, result status, sales order number and time. Updates and
  deletes are forbidden by rule (CLAUDE.md).
- **Every decision** goes into the hash-chained archive `s3://o2c-audit`: the agent's
  `orders.validated`, `orders.posted` and `orders.exceptions` events, and every new
  `review_decisions` row. `ml/verify_audit.py` detects any change, deletion or insertion
  (ADR-014). On 2026-10-28 the chain held 68 entries and was intact (`make audit-check`).
- **Release approvals** are recorded in the Jenkins audit trail (`/input/ProdApproval/
  proceedEmpty by approver`) and in `reports/approval.txt` (started by, approved by).
- **Metrics:**
  - `o2c_orders_total{decided_by="agent"|"reviewer"}` and `o2c_reviews_total{action}`
    count human decisions separately from the agent's;
  - the touchless rate counts agent decisions only (ADR-012);
  - `analytics.mart_reviews` gives the waiting time per decision (ADR-013). It is never
    used to evaluate reviewers ([eu-ai-act.md](eu-ai-act.md) §4).

## 5. Monitoring the overseers' load
`TouchlessRateLow` fires below 50% over one hour with at least 5 orders. That means most
orders need a person, and a prompt, model or master-data change may have made the
automation useless or the queue too long (ADR-012).
