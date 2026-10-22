-- One row per orders.extracted event (extractor): the purchase order as extracted.
select
    event_id,
    idempotency_key,
    occurred_at as extracted_at,
    payload -> 'purchase_order' ->> 'po_number' as po_number,
    payload -> 'purchase_order' ->> 'customer_name' as customer_name,
    payload -> 'purchase_order' ->> 'currency' as currency,
    (payload -> 'purchase_order' ->> 'total_amount')::numeric(14, 2) as total_amount,
    jsonb_array_length(payload -> 'purchase_order' -> 'lines') as line_count,
    coalesce(jsonb_array_length(payload -> 'injection_findings'), 0) as injection_findings
from {{ source('raw', 'events') }}
where event_type = 'orders.extracted'
