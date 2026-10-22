-- One row per day (UTC): documents received, orders extracted that day and their current
-- status, the touchless rate and the cycle time from receipt to final decision.
with

document_days as (
    select
        received_at::date as date_day,
        count(*) as documents
    from {{ ref('fct_documents') }}
    group by received_at::date
),

order_days as (
    select
        extracted_at::date as date_day,
        count(*) as orders,
        count(*) filter (where final_status = 'posted') as posted,
        count(*) filter (where final_status = 'awaiting_review') as awaiting_review,
        count(*) filter (where final_status = 'rejected') as rejected,
        count(*) filter (where touchless) as touchless_orders,
        count(*) filter (where decision is not null) as decided_by_agent,
        round(avg(cycle_time_seconds))::bigint as avg_cycle_time_seconds,
        round(
            percentile_cont(0.9) within group (order by cycle_time_seconds)
        )::bigint as p90_cycle_time_seconds
    from {{ ref('fct_orders') }}
    group by extracted_at::date
)

select
    order_days.avg_cycle_time_seconds,
    order_days.p90_cycle_time_seconds,
    coalesce(document_days.date_day, order_days.date_day) as date_day,
    coalesce(document_days.documents, 0) as documents,
    coalesce(order_days.orders, 0) as orders,
    coalesce(order_days.posted, 0) as posted,
    coalesce(order_days.awaiting_review, 0) as awaiting_review,
    coalesce(order_days.rejected, 0) as rejected,
    coalesce(order_days.touchless_orders, 0) as touchless_orders,
    -- of the orders the agent decided (as the Prometheus touchless rate, ADR-012)
    round(
        order_days.touchless_orders::numeric / nullif(order_days.decided_by_agent, 0), 4
    ) as touchless_rate
from document_days
full outer join order_days
    on document_days.date_day = order_days.date_day
