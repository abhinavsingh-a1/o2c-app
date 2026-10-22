-- One row per review decision: who decided what, and how long the order waited from the
-- first exception to this decision.
select
    reviews.review_id,
    reviews.idempotency_key,
    reviews.po_number,
    reviews.action,
    reviews.reviewer,
    reviews.reviewer_name,
    reviews.result_status,
    reviews.decided_at,
    orders.first_exception_at,
    round(extract(epoch from reviews.decided_at - orders.first_exception_at))::bigint
        as seconds_to_decision
from {{ ref('stg_review_decisions') }} as reviews
left join {{ ref('fct_orders') }} as orders
    on reviews.idempotency_key = orders.idempotency_key
