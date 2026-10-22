import json

import pytest
from o2c_common import metrics
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, InMemoryStore, PermanentError
from prometheus_client import REGISTRY, Counter, Histogram

GROUP = "metrics-test"


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Delta:
    """Change of one sample between creation and .value."""

    def __init__(self, name: str, **labels):
        self.name, self.labels = name, labels
        self.before = sample(name, **labels)

    @property
    def value(self) -> float:
        return sample(self.name, **self.labels) - self.before


# --- label cardinality rule ---

# Values of these would grow with every document or order.
FORBIDDEN_LABELS = {
    "idempotency_key",
    "event_id",
    "correlation_id",
    "po_number",
    "customer",
    "customer_number",
    "filename",
    "s3_key",
    "path",
    "url",
    "sender",
    "user",
    "reviewer",
}


def defined_metrics() -> list:
    """Every metric object defined in o2c_common.metrics."""
    return [m for m in vars(metrics).values() if isinstance(m, Counter | Histogram)]


def test_every_o2c_metric_uses_only_allowed_low_cardinality_labels():
    defined = defined_metrics()
    assert len(defined) == 15
    for metric in defined:
        labels = set(metric._labelnames)
        assert metric._name.startswith("o2c_")
        assert labels <= metrics.ALLOWED_LABELS, (metric._name, labels)
        assert not labels & FORBIDDEN_LABELS


def test_samples_carry_no_identifier_labels(fastapi_client):
    fastapi_client.get("/reviews/" + "k" * 64)  # an idempotency key in the path
    for family in REGISTRY.collect():
        if not family.name.startswith("o2c_"):
            continue
        for s in family.samples:
            assert set(s.labels) - {"le"} <= metrics.ALLOWED_LABELS
            assert all("k" * 64 not in v for v in s.labels.values())


def test_allowed_labels_contain_no_identifiers():
    assert not metrics.ALLOWED_LABELS & FORBIDDEN_LABELS


def test_unknown_http_methods_are_folded_into_other():
    assert metrics.method_label("get") == "GET"
    assert metrics.method_label("PROPFIND") == "other"


def test_observe_http_counts_and_times_a_request():
    count = Delta(
        "o2c_http_requests_total", method="GET", route="/x/{id}", status="200"
    )
    hist = Delta(
        "o2c_http_request_duration_seconds_count", method="GET", route="/x/{id}"
    )

    metrics.observe_http("get", "/x/{id}", 200, 0.02)

    assert (count.value, hist.value) == (1, 1)


# --- FastAPI ---


@pytest.fixture
def fastapi_client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()
    metrics.instrument_fastapi(app)

    @app.get("/reviews/{idempotency_key}")
    def review(idempotency_key: str):
        return {"key": idempotency_key}

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=False)


def test_fastapi_labels_the_route_template_not_the_key(fastapi_client):
    route = "/reviews/{idempotency_key}"
    ok = Delta("o2c_http_requests_total", method="GET", route=route, status="200")

    fastapi_client.get("/reviews/aaaa")
    fastapi_client.get("/reviews/bbbb")

    assert ok.value == 2
    body = fastapi_client.get("/metrics").text
    assert "aaaa" not in body and "bbbb" not in body


def test_fastapi_unknown_paths_are_unmatched_and_errors_are_500(fastapi_client):
    unmatched = Delta(
        "o2c_http_requests_total", method="GET", route="unmatched", status="404"
    )
    error = Delta("o2c_http_requests_total", method="GET", route="/boom", status="500")

    fastapi_client.get("/wp-admin/setup.php")
    fastapi_client.get("/boom")

    assert (unmatched.value, error.value) == (1, 1)


def test_fastapi_metrics_endpoint_is_not_counted(fastapi_client):
    scraped = Delta(
        "o2c_http_requests_total", method="GET", route="/metrics", status="200"
    )

    response = fastapi_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "o2c_http_requests_total" in response.text
    assert scraped.value == 0


# --- Flask ---


def test_flask_labels_the_url_rule():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    metrics.instrument_flask(app)

    @app.post("/intake/<source>")
    def intake(source):
        return {"source": source}, 202

    client = app.test_client()
    accepted = Delta(
        "o2c_http_requests_total",
        method="POST",
        route="/intake/<source>",
        status="202",
    )
    unmatched = Delta(
        "o2c_http_requests_total", method="GET", route="unmatched", status="404"
    )

    client.post("/intake/email")
    client.post("/intake/upload")
    client.get("/nope")
    scrape = client.get("/metrics")

    assert (accepted.value, unmatched.value) == (2, 1)
    assert scrape.status_code == 200
    assert b"o2c_http_requests_total" in scrape.data


def test_metrics_payload_in_multiprocess_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    body, content_type = metrics.metrics_payload()

    assert content_type.startswith("text/plain")
    assert isinstance(body, bytes)


# --- LLM cost ---


class FakeResponse:
    def __init__(self, headers: dict):
        self.headers = headers


def test_llm_cost_hook_adds_litellm_cost_per_service():
    cost = Delta("o2c_llm_cost_usd_total", service="extractor")
    calls = Delta("o2c_llm_calls_total", service="extractor")
    hook = metrics.llm_cost_hook("extractor")

    hook(FakeResponse({"x-litellm-response-cost": "0.0012"}))
    hook(FakeResponse({"x-litellm-response-cost": "0.0008"}))
    hook(FakeResponse({}))  # no LLM call (or no cost): ignored
    hook(FakeResponse({"x-litellm-response-cost": "n/a"}))

    assert cost.value == pytest.approx(0.002)
    assert calls.value == 2


def test_llm_http_client_records_cost_and_waits_for_slow_calls():
    httpx = pytest.importorskip("httpx")
    client = metrics.llm_http_client("agent")
    cost = Delta("o2c_llm_cost_usd_total", service="agent")
    client._transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"x-litellm-response-cost": "0.5"})
    )

    client.get("http://litellm/chat/completions")

    assert cost.value == 0.5
    assert client.timeout.read == 600.0


# --- Kafka consumer (every consumer group gets these) ---


class FakeMessage:
    def __init__(self, value: bytes):
        self._value = value

    def value(self):
        return self._value

    def key(self):
        return b"key"

    def topic(self):
        return "docs.received"

    def partition(self):
        return 0

    def offset(self):
        return 1


class FakeProducer:
    def send_raw(self, topic, key, value, headers=None):
        pass

    def flush(self, timeout: float = 10.0):
        pass


def consumer(handler, store=None) -> EventConsumer:
    return EventConsumer(
        bootstrap="unused:9092",
        group_id=GROUP,
        topics=["docs.received"],
        handler=handler,
        producer=FakeProducer(),
        store=store or InMemoryStore(),
        max_attempts=3,
        base_delay=0,
    )


def message() -> FakeMessage:
    return FakeMessage(Envelope.new("docs.received", "intake", "k" * 64, {}).to_bytes())


def raising(*errors):
    errors = list(errors)

    def handler(event):
        if errors:
            raise errors.pop(0)

    return handler


def consumer_deltas() -> dict[str, Delta]:
    return {
        "consumed": Delta("o2c_events_consumed_total", group=GROUP),
        "failed": Delta("o2c_events_failed_total", group=GROUP),
        "duplicates": Delta("o2c_events_duplicates_total", group=GROUP),
        "handled": Delta("o2c_event_handler_seconds_count", group=GROUP),
        **{
            reason: Delta("o2c_events_dlq_total", group=GROUP, reason=reason)
            for reason in metrics.DLQ_REASONS
        },
    }


def changes(deltas: dict[str, Delta]) -> dict[str, float]:
    return {name: d.value for name, d in deltas.items() if d.value}


def test_success_and_duplicate():
    deltas = consumer_deltas()
    c, msg = consumer(raising()), message()

    c.process(msg)
    c.process(msg)  # redelivered: already processed

    assert changes(deltas) == {"consumed": 2, "handled": 1, "duplicates": 1}


def test_transient_error_then_success():
    deltas = consumer_deltas()

    consumer(raising(RuntimeError("LiteLLM down"))).process(message())

    assert changes(deltas) == {"consumed": 1, "failed": 1, "handled": 2}


def test_permanent_error_goes_to_dlq():
    deltas = consumer_deltas()

    consumer(raising(PermanentError("no PO"))).process(message())

    assert changes(deltas) == {
        "consumed": 1,
        "failed": 1,
        "handled": 1,
        "permanent_error": 1,
    }


def test_exhausted_retries_go_to_dlq():
    deltas = consumer_deltas()

    consumer(raising(*[RuntimeError("x")] * 3)).process(message())

    assert changes(deltas) == {
        "consumed": 1,
        "failed": 3,
        "handled": 3,
        "retries_exhausted": 1,
    }


def test_invalid_envelope_goes_to_dlq():
    deltas = consumer_deltas()

    consumer(raising()).process(FakeMessage(json.dumps({"x": 1}).encode()))

    assert changes(deltas) == {"consumed": 1, "invalid_envelope": 1}


def test_init_labels_creates_every_combination_at_zero():
    metrics.init_labels(metrics.ORDERS, metrics.ORDER_OUTCOMES, metrics.DECIDED_BY)

    for outcome in metrics.ORDER_OUTCOMES:
        for decided_by in metrics.DECIDED_BY:
            value = REGISTRY.get_sample_value(
                "o2c_orders_total", {"outcome": outcome, "decided_by": decided_by}
            )
            assert value is not None  # the series exists (from 0), before any order


def test_a_consumer_creates_its_series_at_zero():
    group = "fresh-group"
    assert (
        REGISTRY.get_sample_value("o2c_events_consumed_total", {"group": group}) is None
    )
    EventConsumer(  # constructed only, no message yet
        bootstrap="unused:9092",
        group_id=group,
        topics=["docs.received"],
        handler=raising(),
        producer=FakeProducer(),
        store=InMemoryStore(),
    )

    for name in ("o2c_events_consumed_total", "o2c_events_failed_total"):
        assert REGISTRY.get_sample_value(name, {"group": group}) == 0
    for reason in metrics.DLQ_REASONS:
        labels = {"group": group, "reason": reason}
        assert REGISTRY.get_sample_value("o2c_events_dlq_total", labels) is not None
