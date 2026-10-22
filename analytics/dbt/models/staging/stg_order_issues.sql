-- One row per issue of the agent's own decision (not of re-validations after a review).
select
    validated.idempotency_key,
    validated.validated_at,
    issue.value ->> 'code' as issue_code,
    (issue.value ->> 'line_no')::integer as line_no
from {{ ref('stg_orders_validated') }} as validated
cross join lateral jsonb_array_elements(validated.issues) as issue (value)
where not validated.after_review
