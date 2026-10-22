"""Prometheus metrics shared by every service (ADR-012).

All o2c_* metrics are defined here, so names and labels are the same in every service
and one test can check the label cardinality rule: every label has a small, fixed set
of values known in advance (route templates, consumer groups, doc types, statuses,
issue codes, review actions, service names). Never use an idempotency key, event id,
PO number, customer, file name or raw URL path as a label value.

Exposition: FastAPI apps get GET /metrics from instrument_fastapi(); intake (Flask under
gunicorn with several workers) uses prometheus_client's multiprocess mode when
PROMETHEUS_MULTIPROC_DIR is set; the classifier serves metrics_payload() on its health
server. Prometheus adds the `job` label per service, so the technical metrics do not
repeat the service name.
"""

import itertools
import os
import time
from collections.abc import Callable, Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

# Label names allowed on o2c_* metrics; each has a bounded set of values.
ALLOWED_LABELS = frozenset(
    {
        "method",  # HTTP method (HTTP_METHODS or "other")
        "route",  # route template such as /reviews/{idempotency_key}, or "unmatched"
        "status",  # HTTP status code
        "group",  # Kafka consumer group
        "reason",  # DLQ reason (DLQ_REASONS)
        "source",  # intake source: email | upload
        "doc_type",  # purchase_order | invoice | remittance | other
        "classified_by",  # distilbert | gpt
        "outcome",  # order status: posted | awaiting_review | rejected | failed
        "decided_by",  # agent | reviewer
        "code",  # agent IssueCode
        "action",  # review action: approve | edit | reject
        "service",  # classifier | extractor | agent
    }
)
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
DLQ_REASONS = ("invalid_envelope", "permanent_error", "retries_exhausted")
UNMATCHED_ROUTE = "unmatched"  # 404s: never the raw path (scanners would explode it)
METRICS_PATH = "/metrics"
SOURCES = ("email", "upload")
DOC_TYPES = ("purchase_order", "invoice", "remittance", "other")
CLASSIFIED_BY = ("distilbert", "gpt")
ORDER_OUTCOMES = ("posted", "awaiting_review", "rejected", "failed")
DECIDED_BY = ("agent", "reviewer")
REVIEW_ACTIONS = ("approve", "edit", "reject")

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120)

# --- technical ---
HTTP_REQUESTS = Counter(
    "o2c_http_requests_total",
    "HTTP requests by route template and status code",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "o2c_http_request_duration_seconds",
    "HTTP request duration by route template",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
EVENTS_CONSUMED = Counter(
    "o2c_events_consumed_total",
    "Kafka messages taken from the topic by a consumer group (each message once)",
    ["group"],
)
EVENTS_FAILED = Counter(
    "o2c_events_failed_total",
    "Handler attempts that raised (retried or sent to the DLQ)",
    ["group"],
)
EVENTS_DLQ = Counter(
    "o2c_events_dlq_total",
    "Messages a consumer group sent to the dead-letter queue",
    ["group", "reason"],
)
EVENTS_DUPLICATE = Counter(
    "o2c_events_duplicates_total",
    "Messages skipped because the event was already processed (idempotency)",
    ["group"],
)
HANDLER_SECONDS = Histogram(
    "o2c_event_handler_seconds",
    "Duration of one handler attempt",
    ["group"],
    buckets=LLM_BUCKETS,
)

# --- business ---
DOCUMENTS_RECEIVED = Counter(
    "o2c_documents_received_total", "Documents accepted by intake", ["source"]
)
DOCUMENTS_CLASSIFIED = Counter(
    "o2c_documents_classified_total",
    "Documents classified, by type and by which model decided",
    ["doc_type", "classified_by"],
)
EXTRACTION_SECONDS = Histogram(
    "o2c_extraction_seconds",
    "Duration of one purchase order extraction (LLM call and parsing)",
    buckets=LLM_BUCKETS,
)
ORDERS = Counter(
    "o2c_orders_total",
    "Order outcomes: decided_by=agent once per order when the agent decides; "
    "decided_by=reviewer for each review decision",
    ["outcome", "decided_by"],
)
ISSUES = Counter(
    "o2c_issues_total", "Issues found by the agent's checks, by issue code", ["code"]
)
REVIEWS = Counter("o2c_reviews_total", "Review decisions by action", ["action"])
LLM_COST = Counter(
    "o2c_llm_cost_usd_total",
    "OpenAI cost in USD from LiteLLM's x-litellm-response-cost header",
    ["service"],
)
LLM_CALLS = Counter(
    "o2c_llm_calls_total", "LLM calls through LiteLLM (with a cost header)", ["service"]
)

COST_HEADER = "x-litellm-response-cost"


def init_labels(metric, *values: Iterable[str]) -> None:
    """Create every combination of the given label values at 0. Prometheus' increase()
    and rate() miss the first increment of a series that appears only then, which
    would undercount e.g. the touchless rate and the LLM cost alerts."""
    for combination in itertools.product(*values):
        metric.labels(*combination)


def init_consumer_metrics(group: str) -> None:
    for metric in (EVENTS_CONSUMED, EVENTS_FAILED, EVENTS_DUPLICATE):
        metric.labels(group)
    init_labels(EVENTS_DLQ, [group], DLQ_REASONS)


def method_label(method: str) -> str:
    method = method.upper()
    return method if method in HTTP_METHODS else "other"


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    method = method_label(method)
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def sample(name: str, **labels: str) -> float:
    """Current value of one sample in this process (0.0 if not recorded yet); for tests."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def metrics_payload() -> tuple[bytes, str]:
    """(body, content type) for GET /metrics. With PROMETHEUS_MULTIPROC_DIR set (intake
    under gunicorn), the values of all worker processes are collected."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def llm_cost_hook(service: str) -> Callable:
    """httpx response hook: adds LiteLLM's cost header to o2c_llm_cost_usd_total."""

    def record(response) -> None:
        cost = response.headers.get(COST_HEADER)
        if cost is None:
            return
        try:
            value = float(cost)
        except ValueError:
            return
        LLM_CALLS.labels(service).inc()
        LLM_COST.labels(service).inc(value)

    return record


def llm_http_client(service: str):
    """httpx client for the OpenAI SDK / ChatOpenAI that records the LLM cost.
    The timeout matches the OpenAI SDK's default (LLM calls can take a minute)."""
    import httpx

    init_labels(LLM_COST, [service])
    init_labels(LLM_CALLS, [service])
    return httpx.Client(
        timeout=httpx.Timeout(600.0, connect=10.0),
        event_hooks={"response": [llm_cost_hook(service)]},
    )


def instrument_fastapi(app) -> None:
    """Request count and latency per route template, and GET /metrics."""
    from fastapi import Response

    @app.middleware("http")
    async def prometheus_middleware(request, call_next):
        if request.url.path == METRICS_PATH:
            return await call_next(request)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", None)
            observe_http(
                request.method,
                route or UNMATCHED_ROUTE,
                status,
                time.perf_counter() - start,
            )

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics() -> Response:
        body, content_type = metrics_payload()
        return Response(body, media_type=content_type)


def instrument_flask(app) -> None:
    """Request count and latency per URL rule, and GET /metrics (Flask)."""
    from flask import Response, g, request

    @app.before_request
    def start_timer():
        g.o2c_metrics_start = time.perf_counter()

    @app.after_request
    def record(response):
        start = g.pop("o2c_metrics_start", None)
        if start is not None and request.path != METRICS_PATH:
            rule = request.url_rule.rule if request.url_rule else UNMATCHED_ROUTE
            observe_http(
                request.method,
                rule,
                response.status_code,
                time.perf_counter() - start,
            )
        return response

    @app.get(METRICS_PATH)
    def metrics():
        body, content_type = metrics_payload()
        return Response(body, content_type=content_type)
