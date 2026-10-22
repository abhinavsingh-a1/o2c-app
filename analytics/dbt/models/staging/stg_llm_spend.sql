-- One row per LLM call through LiteLLM. Calls with the master key have no key alias.
-- session_id is the order's idempotency key for classify, extract and explain calls.
select
    request_id,
    start_time as started_at,
    spend_usd,
    model,
    model_group,
    session_id,
    coalesce(service, 'untagged') as service,
    coalesce(key_alias, 'master') as key_alias
from {{ source('raw', 'llm_spend') }}
