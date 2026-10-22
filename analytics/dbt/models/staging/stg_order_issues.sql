-- One row per issue of the agent's own decision (not of re-validations after a review).
-- Refs are selected in import CTEs: `ref() as alias` breaks `dbt build --empty` (CI).
with

validated as (
    select * from {{ ref('stg_orders_validated') }}
)

select
    validated.idempotency_key,
    validated.validated_at,
    issue.value ->> 'code' as issue_code,
    (issue.value ->> 'line_no')::integer as line_no
from validated
cross join lateral jsonb_array_elements(validated.issues) as issue (value)
where not validated.after_review
