from erp_mock.api import app
from fastapi.testclient import TestClient
from o2c_common.metrics import sample


def test_requests_are_counted_per_route_template_and_metrics_are_exposed():
    client = TestClient(app)
    before = sample(
        "o2c_http_requests_total", method="GET", route="/health", status="200"
    )

    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "o2c_http_request_duration_seconds" in response.text
    after = sample(
        "o2c_http_requests_total", method="GET", route="/health", status="200"
    )
    assert after - before == 1
