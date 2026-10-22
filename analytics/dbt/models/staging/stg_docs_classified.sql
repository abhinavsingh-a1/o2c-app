-- One row per docs.classified event (classifier).
select
    event_id,
    idempotency_key,
    occurred_at as classified_at,
    payload ->> 'doc_type' as doc_type,
    payload ->> 'classified_by' as classified_by,
    payload ->> 'routing_reason' as routing_reason,
    (payload ->> 'confidence')::numeric as confidence
from {{ source('raw', 'events') }}
where event_type = 'docs.classified'
