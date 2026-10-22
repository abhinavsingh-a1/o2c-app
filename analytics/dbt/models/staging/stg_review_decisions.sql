-- One row per review decision (append-only audit trail of the agent's review API).
select
    id as review_id,
    idempotency_key,
    po_number,
    action,
    reviewer,
    reviewer_name,
    result_status,
    decided_at
from {{ source('app', 'review_decisions') }}
