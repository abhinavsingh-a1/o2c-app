import pytest
from agent.settings import REPO_ROOT
from o2c_common.events import Envelope
from o2c_common.kafka import PermanentError
from o2c_common.metrics import sample

# The fakes and the app / client / auth_headers / handle / waiting / make_event /
# document / deviating_po fixtures live in conftest.py.

OTHER_KEY = "f" * 64
PDF = (REPO_ROOT / "poc" / "data" / "pos" / "PO-0001.pdf").read_bytes()
APPROVE = {"action": "approve"}


# --- handler ---


def test_clean_po_publishes_validated_and_posted(handle, producer, erp, po, make_event):
    incoming = make_event(po)

    handle(incoming)

    assert producer.topics() == ["orders.validated", "orders.posted"]
    posted = producer.last("orders.posted")
    assert posted.payload["status"] == "posted"
    assert posted.payload["sales_order_number"] in erp.orders
    assert posted.payload["total_amount"] == str(po.total_amount)
    assert posted.causation_id == incoming.event_id
    assert posted.idempotency_key == incoming.idempotency_key


def test_price_deviation_publishes_validated_and_exception(
    handle, producer, erp, deviating_po, make_event, document
):
    handle(make_event(deviating_po))

    assert producer.topics() == ["orders.validated", "orders.exceptions"]
    exception = producer.last("orders.exceptions").payload
    assert exception["status"] == "awaiting_review"
    assert exception["explanation"] == "Needs review: PRICE_DEVIATION"
    assert exception["source_document"] == document
    assert exception["purchase_order"] == deviating_po.model_dump(mode="json")
    assert [i["code"] for i in exception["issues"]] == ["PRICE_DEVIATION"]
    assert erp.created == []


def test_same_event_twice_gives_identical_event_ids(
    handle, producer, erp, po, make_event
):
    incoming = make_event(po)

    handle(incoming)
    first = [(topic, e.event_id) for topic, e in producer.sent]
    producer.sent.clear()
    handle(incoming)
    second = [(topic, e.event_id) for topic, e in producer.sent]

    assert first == second
    assert len(erp.created) == 1


def test_payload_without_purchase_order_is_permanent_error(
    handle, producer, review_store, document
):
    event = Envelope.new("orders.extracted", "extractor", OTHER_KEY, dict(document))

    with pytest.raises(PermanentError):
        handle(event)
    assert producer.sent == []
    assert review_store.rows == {}


def test_handler_upserts_order_into_review_store(
    handle, review_store, deviating_po, make_event
):
    event = make_event(deviating_po)
    handle(event)

    row = review_store.rows[event.idempotency_key]
    assert row.status == "awaiting_review"
    assert row.po_number == deviating_po.po_number
    assert row.customer_number == "1000107"
    assert row.total_amount == deviating_po.total_amount
    assert row.issue_codes == ["PRICE_DEVIATION"]

    handle(event)  # redelivery updates, never duplicates

    assert list(review_store.rows) == [event.idempotency_key]


# --- API ---


def test_health_returns_ok(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unknown_key_returns_404_on_detail_and_document(client, auth_headers):
    unknown = "u" * 64

    assert client.get(f"/reviews/{unknown}", headers=auth_headers).status_code == 404
    assert (
        client.get(f"/reviews/{unknown}/document", headers=auth_headers).status_code
        == 404
    )


def test_list_reviews_defaults_to_awaiting_review(
    client, auth_headers, handle, po, make_event, waiting
):
    handle(make_event(po, OTHER_KEY))  # clean PO: posted

    resp = client.get("/reviews", headers=auth_headers)

    assert resp.status_code == 200
    assert [r["idempotency_key"] for r in resp.json()] == [waiting]
    assert resp.json()[0]["status"] == "awaiting_review"
    assert resp.json()[0]["issue_codes"] == ["PRICE_DEVIATION"]


def test_list_reviews_with_status_all_returns_everything(
    client, auth_headers, handle, po, make_event, waiting
):
    handle(make_event(po, OTHER_KEY))  # clean PO: posted

    resp = client.get("/reviews", params={"status": "all"}, headers=auth_headers)

    assert resp.status_code == 200
    statuses = {r["idempotency_key"]: r["status"] for r in resp.json()}
    assert statuses == {waiting: "awaiting_review", OTHER_KEY: "posted"}


def test_get_waiting_review(client, auth_headers, waiting, deviating_po, document):
    resp = client.get(f"/reviews/{waiting}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["awaiting_review"] is True
    assert body["status"] == "awaiting_review"
    assert [i["code"] for i in body["issues"]] == ["PRICE_DEVIATION"]
    assert body["source_document"] == document
    assert body["purchase_order"] == deviating_po.model_dump(mode="json")


def test_approve_posts_order_and_publishes(
    client, auth_headers, waiting, producer, erp
):
    resp = client.post(f"/reviews/{waiting}", json=APPROVE, headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "posted"
    assert body["sales_order_number"] in erp.orders
    assert producer.topics() == ["orders.validated", "orders.posted"]
    assert (
        producer.last("orders.posted").payload["sales_order_number"]
        == body["sales_order_number"]
    )
    assert producer.flushes == 1
    detail = client.get(f"/reviews/{waiting}", headers=auth_headers).json()
    assert detail["awaiting_review"] is False


def test_approve_updates_review_store_to_posted(
    client, auth_headers, waiting, review_store
):
    assert review_store.rows[waiting].status == "awaiting_review"

    client.post(f"/reviews/{waiting}", json=APPROVE, headers=auth_headers)

    assert review_store.rows[waiting].status == "posted"
    assert client.get("/reviews", headers=auth_headers).json() == []


def test_get_document_returns_pdf(client, auth_headers, waiting, s3, document):
    s3.objects[(document["s3_bucket"], document["s3_key"])] = PDF

    resp = client.get(f"/reviews/{waiting}/document", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == PDF
    assert 'filename="PO-0001.pdf"' in resp.headers["content-disposition"]


def test_get_document_missing_in_s3_returns_404(client, auth_headers, waiting):
    # nothing stored in FakeS3
    resp = client.get(f"/reviews/{waiting}/document", headers=auth_headers)

    assert resp.status_code == 404


def test_second_review_returns_409(client, auth_headers, waiting, producer, erp):
    assert (
        client.post(
            f"/reviews/{waiting}", json=APPROVE, headers=auth_headers
        ).status_code
        == 200
    )
    producer.sent.clear()

    resp = client.post(f"/reviews/{waiting}", json=APPROVE, headers=auth_headers)

    assert resp.status_code == 409
    assert producer.sent == []
    assert len(erp.created) == 1


def test_reject_sets_status_rejected(client, auth_headers, waiting, producer, erp):
    resp = client.post(
        f"/reviews/{waiting}",
        json={"action": "reject", "comment": "wrong price"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert producer.topics() == ["orders.validated"]
    assert producer.last("orders.validated").payload["status"] == "rejected"
    assert erp.created == []


def test_injection_findings_from_the_extractor_go_to_review(
    handle, producer, erp, po, make_event
):
    event = make_event(po)
    findings = [{"pattern": "pre_approved", "match": "pre-approved"}]
    event = event.model_copy(
        update={"payload": {**event.payload, "injection_findings": findings}}
    )

    handle(event)

    assert producer.topics() == ["orders.validated", "orders.exceptions"]
    exception = producer.last("orders.exceptions").payload
    assert [i["code"] for i in exception["issues"]] == ["PROMPT_INJECTION_SUSPECTED"]
    assert erp.created == []


# --- metrics ---


def orders(outcome: str, decided_by: str) -> float:
    return sample("o2c_orders_total", outcome=outcome, decided_by=decided_by)


def test_agent_outcomes_and_issues_are_counted(handle, po, deviating_po, make_event):
    posted, waiting = orders("posted", "agent"), orders("awaiting_review", "agent")
    deviations = sample("o2c_issues_total", code="PRICE_DEVIATION")

    handle(make_event(po))
    handle(make_event(deviating_po, key=OTHER_KEY))

    assert orders("posted", "agent") - posted == 1  # touchless
    assert orders("awaiting_review", "agent") - waiting == 1
    assert sample("o2c_issues_total", code="PRICE_DEVIATION") - deviations == 1


@pytest.mark.parametrize("which", ["posted", "waiting"])
def test_a_redelivered_event_does_not_count_the_order_twice(
    handle, po, deviating_po, make_event, which
):
    event = make_event(po if which == "posted" else deviating_po)
    outcome = "posted" if which == "posted" else "awaiting_review"
    before = orders(outcome, "agent")

    handle(event)
    handle(event)

    assert orders(outcome, "agent") - before == 1


def test_review_decisions_are_counted(client, auth_headers, waiting):
    approvals = sample("o2c_reviews_total", action="approve")
    posted_by_reviewer = orders("posted", "reviewer")
    posted_by_agent = orders("posted", "agent")

    client.post(f"/reviews/{waiting}", json=APPROVE, headers=auth_headers)

    assert sample("o2c_reviews_total", action="approve") - approvals == 1
    assert orders("posted", "reviewer") - posted_by_reviewer == 1
    assert orders("posted", "agent") == posted_by_agent  # not touchless


def test_metrics_use_the_route_template_not_the_idempotency_key(
    client, auth_headers, waiting
):
    labels = {"method": "GET", "route": "/reviews/{idempotency_key}", "status": "200"}
    before = sample("o2c_http_requests_total", **labels)

    client.get(f"/reviews/{waiting}", headers=auth_headers)
    body = client.get("/metrics").text

    assert sample("o2c_http_requests_total", **labels) - before == 1
    assert waiting not in body
