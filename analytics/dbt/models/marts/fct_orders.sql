-- One row per idempotency key with an extracted purchase order: the agent's decision,
-- the final status and the times in between.
-- touchless: posted, and no review decision was made (no audit row and no event the
-- agent emitted after a review).
with

extracted as (
    select distinct on (idempotency_key)
        idempotency_key,
        extracted_at,
        po_number,
        customer_name,
        currency,
        total_amount
    from {{ ref('stg_orders_extracted') }}
    order by idempotency_key asc, extracted_at asc
),

agent_decision as (
    select distinct on (idempotency_key)
        idempotency_key,
        decision,
        validated_at as agent_decided_at
    from {{ ref('stg_orders_validated') }}
    where not after_review
    order by idempotency_key asc, validated_at asc
),

latest as (
    select distinct on (idempotency_key)
        idempotency_key,
        status as final_status,
        customer_number,
        validated_at as last_validated_at
    from {{ ref('stg_orders_validated') }}
    order by idempotency_key asc, validated_at desc
),

review_events as (
    select
        idempotency_key,
        bool_or(after_review) as has_review_event
    from {{ ref('stg_orders_validated') }}
    group by idempotency_key
),

posted as (
    select
        idempotency_key,
        min(posted_at) as posted_at,
        min(sales_order_number) as sales_order_number
    from {{ ref('stg_orders_posted') }}
    group by idempotency_key
),

exceptions as (
    select
        idempotency_key,
        min(exception_at) as first_exception_at
    from {{ ref('stg_orders_exceptions') }}
    group by idempotency_key
),

reviews as (
    select
        idempotency_key,
        count(*) as review_decisions
    from {{ ref('stg_review_decisions') }}
    group by idempotency_key
),

issues as (
    select
        idempotency_key,
        count(*) as issue_count,
        string_agg(distinct issue_code, ',' order by issue_code) as issue_codes
    from {{ ref('stg_order_issues') }}
    group by idempotency_key
),

joined as (
    select
        extracted.idempotency_key,
        extracted.po_number,
        latest.customer_number,
        extracted.customer_name,
        extracted.currency,
        extracted.total_amount,
        documents.received_at,
        extracted.extracted_at,
        agent_decision.decision,
        agent_decision.agent_decided_at,
        posted.posted_at,
        posted.sales_order_number,
        exceptions.first_exception_at,
        issues.issue_codes,
        coalesce(latest.final_status, 'in_progress') as final_status,
        coalesce(reviews.review_decisions, 0) as review_decisions,
        coalesce(issues.issue_count, 0) as issue_count,
        coalesce(reviews.review_decisions, 0) > 0
        or coalesce(review_events.has_review_event, false) as reviewed,
        case
            when latest.final_status = 'posted' then posted.posted_at
            when latest.final_status in ('rejected', 'failed') then latest.last_validated_at
        end as decided_at
    from extracted
    left join {{ ref('fct_documents') }} as documents
        on extracted.idempotency_key = documents.idempotency_key
    left join agent_decision
        on extracted.idempotency_key = agent_decision.idempotency_key
    left join latest
        on extracted.idempotency_key = latest.idempotency_key
    left join review_events
        on extracted.idempotency_key = review_events.idempotency_key
    left join posted
        on extracted.idempotency_key = posted.idempotency_key
    left join exceptions
        on extracted.idempotency_key = exceptions.idempotency_key
    left join reviews
        on extracted.idempotency_key = reviews.idempotency_key
    left join issues
        on extracted.idempotency_key = issues.idempotency_key
)

select
    idempotency_key,
    po_number,
    customer_number,
    customer_name,
    currency,
    total_amount,
    received_at,
    extracted_at,
    decision,
    agent_decided_at,
    final_status,
    posted_at,
    sales_order_number,
    first_exception_at,
    decided_at,
    review_decisions,
    reviewed,
    issue_count,
    issue_codes,
    final_status = 'posted' and not reviewed as touchless,
    round(extract(epoch from decided_at - coalesce(received_at, extracted_at)))::bigint
        as cycle_time_seconds
from joined
