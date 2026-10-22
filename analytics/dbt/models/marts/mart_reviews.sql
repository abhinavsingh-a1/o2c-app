-- One row per review decision: who decided what, and how long the order waited from the
-- first exception to this decision.
with

reviews as (
    select * from {{ ref('stg_review_decisions') }}
),

orders as (
    select * from {{ ref('fct_orders') }}
)

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
from reviews
left join orders
    on reviews.idempotency_key = orders.idempotency_key
