-- One row per day (UTC), LiteLLM key alias, calling service and model: LLM cost and
-- calls by category (ADR-013):
--   order:      session_id is an order's idempotency key (classify, extract, explain)
--   document:   session_id is the key of a received document that is not an order
--               (e.g. classifying an invoice)
--   shared:     agent calls that serve many orders (contract tolerance lookup, material
--               matching, embeddings) and carry no document session
--   evaluation: everything else - the eval key or tag, evaluation sessions (eval-,
--               redteam-, judge-) and classifier/extractor calls for no real document
-- cost_per_order_usd = order cost / orders extracted that day; the rows of one day add up
-- to that day's LLM cost per order.
with

llm_spend as (
    select * from {{ ref('stg_llm_spend') }}
),

documents as (
    select * from {{ ref('fct_documents') }}
),

orders as (
    select
        idempotency_key,
        extracted_at::date as date_day
    from {{ ref('fct_orders') }}
),

order_days as (
    select
        date_day,
        count(*) as orders
    from orders
    group by date_day
),

calls as (
    select
        spend.started_at::date as date_day,
        spend.key_alias,
        spend.service,
        spend.model,
        spend.spend_usd,
        case
            when
                spend.key_alias = 'eval'
                or spend.service = 'eval'
                or spend.session_id ~ '^(eval|redteam|judge)-'
                then 'evaluation'
            when orders.idempotency_key is not null then 'order'
            when documents.idempotency_key is not null then 'document'
            when spend.service = 'agent' then 'shared'
            else 'evaluation'
        end as cost_category
    from llm_spend as spend
    left join orders
        on spend.session_id = orders.idempotency_key
    left join documents
        on spend.session_id = documents.idempotency_key
),

spend as (
    select
        date_day,
        key_alias,
        service,
        model,
        sum(spend_usd) as cost_usd,
        count(*) as calls,
        coalesce(sum(spend_usd) filter (where cost_category = 'order'), 0)
            as order_cost_usd,
        coalesce(sum(spend_usd) filter (where cost_category = 'document'), 0)
            as document_cost_usd,
        coalesce(sum(spend_usd) filter (where cost_category = 'shared'), 0)
            as shared_cost_usd,
        coalesce(sum(spend_usd) filter (where cost_category = 'evaluation'), 0)
            as evaluation_cost_usd
    from calls
    group by date_day, key_alias, service, model
)

select
    spend.date_day,
    spend.key_alias,
    spend.service,
    spend.model,
    spend.cost_usd,
    spend.calls,
    spend.order_cost_usd,
    spend.document_cost_usd,
    spend.shared_cost_usd,
    spend.evaluation_cost_usd,
    coalesce(order_days.orders, 0) as orders,
    spend.order_cost_usd / nullif(order_days.orders, 0) as cost_per_order_usd
from spend
left join order_days
    on spend.date_day = order_days.date_day
