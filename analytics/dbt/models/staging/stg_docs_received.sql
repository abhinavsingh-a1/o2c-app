-- One row per docs.received event (intake). The sender (an e-mail address) is left out.
select
    event_id,
    idempotency_key,
    occurred_at as received_at,
    payload ->> 'source' as intake_source,
    payload ->> 'filename' as filename,
    (payload ->> 'size_bytes')::integer as size_bytes
from {{ source('raw', 'events') }}
where event_type = 'docs.received'
