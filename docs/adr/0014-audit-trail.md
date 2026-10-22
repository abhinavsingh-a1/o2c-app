# ADR-014: Tamper-evident audit trail

Date: 2026-10-22
Status: Accepted

## Context
The agent posts orders in the ERP, or stops them for a person (ADR-004), and reviewers
approve, edit or reject orders. Auditors (finance, EU AI Act human oversight) must be
able to check afterwards which decision was made for which order, by the agent or by
whom, and that this record was not changed later.
- `review_decisions` is append-only by rule only: a database owner can still update it.
- Kafka keeps events for 7 to 30 days (ADR-003).
- `raw.events` (ADR-013) is a copy for analytics, not evidence.

## Decision

### Hash chain (services/audit-archiver, locally and in every environment)
- **What is archived:** every decision becomes one JSON line in
  `s3://o2c-audit/<yyyy>/<mm>/<dd>.jsonl`. That covers the agent's `orders.validated`,
  `orders.posted` and `orders.exceptions` events (consumer group `audit-archiver`) and
  every new row of `public.review_decisions` (polled every 10 s).
- **Fields:** `seq`, `timestamp` (archive time, UTC), `source` (topic or
  `review_decisions`), `idempotency_key`, `record` (the whole event or row),
  `prev_hash`, and `hash`.
- **How the hash is computed:** `hash = sha256(prev_hash + canonical JSON of the entry
  without hash)`. Canonical JSON has sorted keys, no whitespace and UTF-8.
  - The hash covers every field, not only `record`, which goes beyond the first
    specification. Otherwise `seq`, `timestamp`, `source` or `idempotency_key` could be
    changed without breaking the chain.
- **One chain across days:** `seq` counts from 1 without gaps, and the first entry of a
  day links to the last entry of the day before.
- **Idempotent per event id and review id:**
  - An index table `audit_archive` holds the source id of every archived entry.
  - S3 objects cannot be appended to, so the day's object is read and written back
    under a process lock. The S3 write comes before the index write.
  - At start the archiver loads the chain head from the newest object and adds any of
    its entries the index lacks. After a crash between the two writes, a redelivered
    event is still skipped.
  - Verified locally: a restart continued at seq 47 without duplicates.
- **Verification:** `ml/verify_audit.py` walks the whole chain and prints the first
  broken entry (object, line, expected seq, reason), exiting 1. Tests check that:
  - an intact chain verifies, also across days;
  - a changed field breaks at that entry;
  - a changed entry with a recomputed hash breaks at the next entry's `prev_hash`;
  - a deleted entry breaks where it is missing, also at a day boundary.

### Object Lock on AWS (o2c-infra `terraform/modules/audit`)
- **The archive bucket** (`<prefix>-audit-archive-<account>`) has S3 Object Lock in
  GOVERNANCE mode with versioning, so a written version cannot be overwritten or
  deleted before its retention date by ordinary principals. A rewrite of the day
  object creates a new version; earlier versions stay.
- **The same module** records AWS-level changes:
  - CloudTrail management events, with log file validation (signed digests);
  - AWS Config for EKS clusters, RDS instances, S3 buckets and IAM roles.
- **The module is behind `enable_audit` (default false)** in `envs/dev`. It is written and
  validated, never applied from here.
- **Retention is 1 day** because every cloud environment must be destroyable when not
  in use (project plan; the buckets have `force_destroy`). Production uses COMPLIANCE
  mode and the legal retention period.

### What auditors can verify
1. **Completeness and order:** `seq` has no gaps, so no decision was removed or
   inserted in between.
2. **Integrity:** every entry's hash matches its content and the previous hash. A
   change anywhere invalidates that entry or the next link. Recomputing all following
   hashes would be needed to hide a change; Object Lock prevents that on AWS, since the
   old versions stay.
3. **The head hash:** an auditor who recorded the head hash (e.g. in a report) can later
   prove that the archive up to that point is unchanged. Publishing it regularly to a
   second system makes even a rewrite of the whole chain detectable.
4. **Content:** each decision: the agent's status, decision and issues (from the
   events), and the reviewer, action, comment and PO before and after (from
   `review_decisions`), each linked by `idempotency_key` to the source document.
5. **AWS changes:** who changed infrastructure (CloudTrail, with validated digests),
   and how the EKS, RDS, S3 and IAM configuration changed over time (Config).

## Consequences
- **One archiver writes the chain.** Several replicas would need a distributed lock or
  a single writer partition; one instance is enough for the volume.
- **Each append rewrites the day's object.** Fine for thousands of decisions a day;
  higher volumes would roll objects per hour.
- **The archive holds order data** (customer names, amounts). Access to the bucket is
  restricted like the documents bucket.
- **The chain proves integrity after archiving,** not that the archiver saw every
  event. That comes from Kafka (the consumer commits only after the append) and from
  reconciling with `raw.events`.
- **Decisions made before the archiver ran are archived at its first start,** because it
  reads the topics from the earliest offset. Their `timestamp` is the archive time; the
  record keeps the original `occurred_at`.
