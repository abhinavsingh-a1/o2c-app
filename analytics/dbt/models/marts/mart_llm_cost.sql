-- One row per day (UTC), LiteLLM key alias and model: LLM cost and calls, and the cost
-- per order extracted that day. Only the pipeline keys (classifier, extractor, agent)
-- are order costs; eval and master-key calls are shown but not divided by orders.
with

spend as (
    select
        started_at::date as date_day,
        key_alias,
        model,
        sum(spend_usd) as cost_usd,
        count(*) as calls
    from {{ ref('stg_llm_spend') }}
    group by started_at::date, key_alias, model
),

order_days as (
    select
        extracted_at::date as date_day,
        count(*) as orders
    from {{ ref('fct_orders') }}
    group by extracted_at::date
)

select
    spend.date_day,
    spend.key_alias,
    spend.model,
    spend.cost_usd,
    spend.calls,
    spend.key_alias in ('classifier', 'extractor', 'agent') as is_pipeline,
    coalesce(order_days.orders, 0) as orders,
    case
        when spend.key_alias in ('classifier', 'extractor', 'agent')
            then spend.cost_usd / nullif(order_days.orders, 0)
    end as cost_per_order_usd
from spend
left join order_days
    on spend.date_day = order_days.date_day
