-- One row per orders.posted event (agent): the sales order in the ERP.
select
    event_id,
    idempotency_key,
    occurred_at as posted_at,
    payload ->> 'sales_order_number' as sales_order_number,
    (payload ->> 'total_amount')::numeric(14, 2) as total_amount,
    payload ->> 'currency' as currency,
    causation_id is null as after_review
from {{ source('raw', 'events') }}
where event_type = 'orders.posted'
