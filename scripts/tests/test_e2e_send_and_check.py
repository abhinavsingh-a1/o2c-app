import argparse
import base64
import json
from io import BytesIO
from pathlib import Path

import e2e_send_and_check as e2e
import httpx
import pytest
from intake.app import sign as intake_sign
from o2c_common.ids import idempotency_key as intake_key
from o2c_common.pdf import pdf_text
from pypdf import PdfReader

PO = (
    Path(__file__).resolve().parents[2]
    / "ml"
    / "data"
    / "train"
    / "pos"
    / "PO-0001.pdf"
)

METRICS = """\
# HELP o2c_orders_total Order outcomes
# TYPE o2c_orders_total counter
o2c_orders_total{outcome="posted",decided_by="agent"} 3.0
o2c_orders_total{outcome="awaiting_review",decided_by="agent"} 2.0
o2c_orders_total{outcome="posted",decided_by="reviewer"} 7.0
o2c_orders_created 1.7e+09
o2c_reviews_total{action="approve"} 1.0
"""


# --- parsing --------------------------------------------------------------------------


def test_parse_labels_with_escapes():
    assert e2e.parse_labels(r'a="1",b="x\"y",c="back\\slash"') == {
        "a": "1",
        "b": 'x"y',
        "c": "back\\slash",
    }


def test_parse_labels_empty():
    assert e2e.parse_labels("") == {}


def test_metric_sum_filters_by_labels():
    assert e2e.metric_sum(METRICS, "o2c_orders_total", {"decided_by": "agent"}) == 5.0


def test_metric_sum_without_labels_sums_every_sample():
    assert e2e.metric_sum(METRICS, "o2c_orders_total") == 12.0


def test_metric_sum_is_zero_when_no_sample_matches():
    assert e2e.metric_sum(METRICS, "o2c_orders_total", {"decided_by": "nobody"}) == 0.0


def test_metric_sum_does_not_match_a_prefix():
    # o2c_orders_created must not count as o2c_orders
    with pytest.raises(ValueError, match="o2c_orders not found"):
        e2e.metric_sum(METRICS, "o2c_orders")


def test_metric_sum_unlabelled_sample():
    assert e2e.metric_sum("up 1\n", "up") == 1.0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://u:p@host:5432/events",
            "postgresql://u:p@host:5432/events",
        ),
        ("postgresql://u:p@host/events", "postgresql://u:p@host/events"),
    ],
)
def test_libpq_url(url, expected):
    assert e2e.libpq_url(url) == expected


def test_parse_expect():
    assert e2e.parse_expect(" posted, awaiting_review ,") == {
        "posted",
        "awaiting_review",
    }


@pytest.mark.parametrize("text", ["", " , ", "posted,failed", "rejected"])
def test_parse_expect_rejects_empty_and_failures(text):
    with pytest.raises(argparse.ArgumentTypeError):
        e2e.parse_expect(text)


# --- request --------------------------------------------------------------------------


def test_unique_pdf_is_the_same_document_with_a_new_key():
    original = PO.read_bytes()

    first, second = (
        e2e.unique_pdf(original, "build-1"),
        e2e.unique_pdf(original, "build-2"),
    )

    assert len({intake_key("upload", b) for b in (original, first, second)}) == 3
    assert first.endswith(b"\n% o2c-e2e build-1\n")
    assert pdf_text(first) == pdf_text(original)
    assert len(PdfReader(BytesIO(first)).pages) == len(
        PdfReader(BytesIO(original)).pages
    )


def test_unique_pdf_rejects_non_pdfs():
    with pytest.raises(ValueError, match="not a PDF"):
        e2e.unique_pdf(b"hello", "build-1")


@pytest.mark.parametrize("run_id", ["", "a b", "x\n", "a/b", "x" * 101])
def test_unique_pdf_rejects_unsafe_run_ids(run_id):
    with pytest.raises(ValueError, match="run id"):
        e2e.unique_pdf(b"%PDF-1.7\n", run_id)


def test_sign_matches_intake():
    assert e2e.sign("secret", b"body") == intake_sign("secret", b"body")


def test_idempotency_key_matches_intake():
    assert e2e.idempotency_key(b"%PDF-x") == intake_key("upload", b"%PDF-x")


def test_intake_body():
    body = json.loads(e2e.intake_body(b"%PDF-x", "PO-0001.pdf", "run-7"))
    assert body == {
        "source": "upload",
        "filename": "PO-0001.pdf",
        "sender": "e2e+run-7@example.com",
        "content_base64": "JVBERi14",
    }


# --- checks ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_for_returns_the_first_result():
    answers, clock = iter([None, None, "done"]), FakeClock()
    assert e2e.wait_for(lambda: next(answers), 60, 5, clock, clock.sleep) == "done"
    assert clock.now == 10


def test_wait_for_gives_up_at_the_timeout():
    clock = FakeClock()
    assert e2e.wait_for(lambda: None, 12, 5, clock, clock.sleep) is None
    assert clock.now == 15  # probes at 0, 5, 10, 15


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_health_passes_when_every_url_answers_200(capsys):
    http = client(lambda request: httpx.Response(200))
    assert e2e.main(["health", "http://a/health", "http://b/"], client=http) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PASS health http://a/health",
        "PASS health http://b/",
        "PASS",
    ]


def test_health_fails_when_a_url_never_answers_200(capsys):
    def handler(request):
        return httpx.Response(200 if request.url.host == "a" else 503)

    http = client(handler)
    assert (
        e2e.main(["health", "http://a/", "http://b/", "--timeout", "0"], client=http)
        == 1
    )
    assert "FAIL health http://b/" in capsys.readouterr().out


@pytest.fixture
def order_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INTAKE_HMAC_SECRET", "secret")
    monkeypatch.setenv("EVENTS_DATABASE_URL", "postgresql+psycopg://u:p@db/events")
    monkeypatch.setattr(e2e.time, "sleep", lambda s: None)
    pdf = tmp_path / "PO-0001.pdf"
    pdf.write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")
    return pdf


def order_args(pdf) -> list[str]:
    return [
        "order",
        "--intake-url", "http://intake/intake",
        "--metrics-url", "http://agent/metrics",
        "--pdf", str(pdf),
        "--run-id", "promote-to-test-12",
        "--timeout", "0",
    ]  # fmt: skip


class FakeStack:
    """intake and the agent's metrics; the agent decides once intake has the order."""

    def __init__(self, status="posted"):
        self.status, self.orders, self.received = status, 4, None

    def handler(self, request):
        if request.url.path == "/intake":
            body = request.content
            assert request.headers["X-O2C-Signature"] == intake_sign("secret", body)
            pdf = base64.b64decode(json.loads(body)["content_base64"])
            self.received = intake_key("upload", pdf)
            self.orders += 1
            return httpx.Response(202, json={"idempotency_key": self.received})
        line = f'o2c_orders_total{{outcome="posted",decided_by="agent"}} {self.orders}'
        return httpx.Response(200, text=line + "\n")

    def review_status(self, database_url, key):
        assert database_url == "postgresql+psycopg://u:p@db/events"
        return self.status if key == self.received else None


def test_order_passes_when_the_agent_decides(monkeypatch, order_env, capsys):
    stack = FakeStack("awaiting_review")
    monkeypatch.setattr(e2e, "review_status", stack.review_status)

    assert e2e.main(order_args(order_env), client=client(stack.handler)) == 0

    out = capsys.readouterr().out
    assert "PASS intake accepted PO-0001.pdf (HTTP 202" in out
    assert "PASS agent decided the order: awaiting_review" in out
    assert 'PASS o2c_orders_total{decided_by="agent"} 4 -> 5' in out


def test_order_fails_when_the_agent_fails_the_order(monkeypatch, order_env, capsys):
    stack = FakeStack("failed")
    monkeypatch.setattr(e2e, "review_status", stack.review_status)

    assert e2e.main(order_args(order_env), client=client(stack.handler)) == 1
    assert "FAIL agent decided the order: failed" in capsys.readouterr().out


def test_order_fails_without_a_decision(monkeypatch, order_env, capsys):
    stack = FakeStack()
    monkeypatch.setattr(e2e, "review_status", lambda url, key: None)

    assert e2e.main(order_args(order_env), client=client(stack.handler)) == 1
    assert "FAIL agent decided the order: no decision" in capsys.readouterr().out


def test_order_fails_when_intake_rejects(monkeypatch, order_env, capsys):
    http = client(
        lambda request: (
            httpx.Response(401, json={"error": "invalid signature"})
            if request.url.path == "/intake"
            else httpx.Response(200, text='o2c_orders_total{decided_by="agent"} 1\n')
        )
    )
    assert e2e.main(order_args(order_env), client=http) == 1
    assert "FAIL intake accepted PO-0001.pdf (HTTP 401" in capsys.readouterr().out


def test_order_fails_when_the_metrics_url_is_wrong(order_env, capsys):
    http = client(lambda request: httpx.Response(200, text="up 1\n"))
    assert e2e.main(order_args(order_env), client=http) == 1
    assert (
        "FAIL ValueError: metric o2c_orders_total not found" in capsys.readouterr().out
    )


def test_missing_setting_fails_without_printing_values(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("INTAKE_HMAC_SECRET", raising=False)
    with pytest.raises(SystemExit, match="INTAKE_HMAC_SECRET is not set"):
        e2e.setting("INTAKE_HMAC_SECRET")


def test_setting_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INTAKE_HMAC_SECRET", raising=False)
    (tmp_path / ".env").write_text("INTAKE_HMAC_SECRET='from-dotenv'\n", "utf-8")
    assert e2e.setting("INTAKE_HMAC_SECRET") == "from-dotenv"
