-- One row per day and issue code of the agent's checks: how often it occurred and its
-- share of all issues that day.
with

counts as (
    select
        validated_at::date as date_day,
        issue_code,
        count(*) as issue_count,
        count(distinct idempotency_key) as orders_affected
    from {{ ref('stg_order_issues') }}
    group by validated_at::date, issue_code
)

select
    date_day,
    issue_code,
    issue_count,
    orders_affected,
    round(issue_count::numeric / sum(issue_count) over (partition by date_day), 4) as share_of_day
from counts
