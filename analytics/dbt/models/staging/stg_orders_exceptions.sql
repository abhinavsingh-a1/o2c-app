-- One row per orders.exceptions event (agent): an order sent to human review.
select
    event_id,
    idempotency_key,
    occurred_at as exception_at,
    causation_id is null as after_review
from {{ source('raw', 'events') }}
where event_type = 'orders.exceptions'
