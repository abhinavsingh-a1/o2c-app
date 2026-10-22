-- One row per orders.validated event (agent). The agent's own decision is caused by
-- orders.extracted; the events after a review decision have no causation_id
-- (agent.app.outcome_events(after, None)).
select
    event_id,
    idempotency_key,
    occurred_at as validated_at,
    payload ->> 'status' as status,
    payload ->> 'decision' as decision,
    payload ->> 'po_number' as po_number,
    payload ->> 'customer_number' as customer_number,
    payload -> 'issues' as issues,
    causation_id is null as after_review
from {{ source('raw', 'events') }}
where event_type = 'orders.validated'
