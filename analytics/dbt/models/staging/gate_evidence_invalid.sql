-- DO NOT MERGE: gate evidence (data). Selects a column stg_docs_received does not have,
-- so dbt build fails in Postgres (the SQL itself parses, so sqlfluff stays green).
-- In staging, not marts: analytics/tests checks the exact set of marts (python gate).
select no_such_column_on_purpose
from {{ ref('stg_docs_received') }}
