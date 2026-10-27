import re

import pytest
from agent.security import (
    API_CSP,
    DOCS_CSP,
    DOCS_HTML,
    HTML_HEADERS,
    SECURITY_HEADERS,
    SecurityHeadersMiddleware,
    add_security,
)
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

# The app / client / auth_headers fixtures live in conftest.py.


def assert_security_headers(resp) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value


def test_json_response_has_security_headers_and_api_csp(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert_security_headers(resp)
    assert resp.headers["Content-Security-Policy"] == API_CSP
    assert "Cross-Origin-Embedder-Policy" not in resp.headers


def test_error_response_has_security_headers(client):
    resp = client.get("/reviews")

    assert resp.status_code == 401
    assert_security_headers(resp)
    assert resp.headers["Content-Security-Policy"] == API_CSP


def test_not_found_has_security_headers(client):
    resp = client.get("/no-such-path")

    assert resp.status_code == 404
    assert_security_headers(resp)


def test_openapi_json_has_api_csp(client):
    resp = client.get("/openapi.json")

    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == API_CSP
    assert "/docs" not in resp.json()["paths"]


def test_docs_is_html_with_docs_csp_and_isolation(client):
    resp = client.get("/docs")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert_security_headers(resp)
    assert resp.headers["Content-Security-Policy"] == DOCS_CSP
    for name, value in HTML_HEADERS.items():
        assert resp.headers[name] == value


def test_docs_page_has_no_inline_script_and_no_cross_domain_includes():
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", DOCS_HTML, re.DOTALL)
    assert len(scripts) == DOCS_HTML.count("<script src=") == 2
    assert all(body == "" for body in scripts)
    assert "//" not in DOCS_HTML


@pytest.mark.parametrize(
    "path, media_type",
    [
        ("/docs/init.js", "text/javascript"),
        ("/docs/static/swagger-ui-bundle.js", "text/javascript"),
        ("/docs/static/swagger-ui.css", "text/css"),
        ("/docs/static/favicon-32x32.png", "image/png"),
    ],
)
def test_docs_assets_are_served_by_the_agent(client, path, media_type):
    resp = client.get(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(media_type)
    assert_security_headers(resp)


def test_redoc_is_disabled(client):
    assert client.get("/redoc").status_code == 404


def test_middleware_keeps_headers_a_route_sets_itself():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/framed")
    def framed() -> Response:
        return Response("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    resp = TestClient(app).get("/framed")

    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_add_security_on_a_bare_app():
    app = FastAPI(docs_url=None, redoc_url=None)
    add_security(app)

    resp = TestClient(app).get("/docs")

    assert resp.status_code == 200
    assert resp.headers["Content-Security-Policy"] == DOCS_CSP
