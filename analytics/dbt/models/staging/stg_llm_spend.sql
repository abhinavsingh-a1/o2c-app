-- One row per LLM call through LiteLLM. Calls with the master key have no key alias.
select
    request_id,
    start_time as started_at,
    spend_usd,
    model,
    model_group,
    coalesce(key_alias, 'master') as key_alias
from {{ source('raw', 'llm_spend') }}
