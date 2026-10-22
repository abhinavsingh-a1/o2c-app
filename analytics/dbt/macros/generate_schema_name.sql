{#- Use the configured schema name as is (staging, analytics), not dbt's default
    "<target schema>_<custom schema>". -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name is not none else target.schema }}
{%- endmacro %}
