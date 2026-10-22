-- One row per received document (idempotency key): when it arrived and how it was
-- classified (the latest classification if it was classified more than once).
with

received as (
    select distinct on (idempotency_key)
        idempotency_key,
        received_at,
        intake_source,
        filename,
        size_bytes
    from {{ ref('stg_docs_received') }}
    order by idempotency_key asc, received_at asc
),

classified as (
    select distinct on (idempotency_key)
        idempotency_key,
        classified_at,
        doc_type,
        classified_by,
        routing_reason,
        confidence
    from {{ ref('stg_docs_classified') }}
    order by idempotency_key asc, classified_at desc
)

select
    received.idempotency_key,
    received.received_at,
    received.intake_source,
    received.filename,
    received.size_bytes,
    classified.classified_at,
    classified.doc_type,
    classified.classified_by,
    classified.routing_reason,
    classified.confidence
from received
left join classified
    on received.idempotency_key = classified.idempotency_key
