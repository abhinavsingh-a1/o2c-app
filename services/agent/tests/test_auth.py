import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
from agent.auth import hash_password, verify_password

# The fakes and the app / client / auth_headers / credentials / waiting fixtures
# live in conftest.py.


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- passwords ---


def test_hash_and_verify_password_round_trip():
    stored = hash_password("correct-horse-battery")

    assert stored.startswith("scrypt$")
    assert "correct-horse-battery" not in stored
    assert verify_password("correct-horse-battery", stored)


def test_verify_password_rejects_wrong_password():
    stored = hash_password("correct-horse-battery")

    assert not verify_password("wrong-password", stored)
    assert not verify_password("correct-horse-battery", "not-a-hash")


def test_same_password_hashes_differently():
    assert hash_password("same-password") != hash_password(
        "same-password"
    )  # random salt


# --- login and tokens ---


def test_login_returns_token_and_user(client, credentials):
    resp = client.post(
        "/auth/login",
        json={"username": credentials["username"], "password": credentials["password"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"] == {
        "username": credentials["username"],
        "name": credentials["name"],
    }
    me = client.get("/auth/me", headers=bearer(body["access_token"]))
    assert me.json()["username"] == credentials["username"]


@pytest.mark.parametrize(
    "username, password",
    [
        ("reviewer1", "wrong-password"),
        ("nobody", "correct-horse-battery"),
    ],
)
def test_login_with_wrong_credentials_returns_401(client, username, password):
    resp = client.post("/auth/login", json={"username": username, "password": password})

    assert resp.status_code == 401
    assert "access_token" not in resp.json()


def test_reviews_without_token_returns_401(client):
    assert client.get("/reviews").status_code == 401


def test_expired_token_returns_401(client, credentials):
    token = jwt.encode(
        {
            "sub": credentials["username"],
            "name": credentials["name"],
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        credentials["jwt_secret"],
        algorithm="HS256",
    )

    assert client.get("/reviews", headers=bearer(token)).status_code == 401


def test_token_signed_with_other_secret_returns_401(client, credentials):
    token = jwt.encode(
        {
            "sub": credentials["username"],
            "name": credentials["name"],
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "some-other-secret-at-least-32-bytes-long",
        algorithm="HS256",
    )

    assert client.get("/reviews", headers=bearer(token)).status_code == 401


def test_tampered_token_returns_401(client, auth_headers):
    header, payload, signature = (
        auth_headers["Authorization"].removeprefix("Bearer ").split(".")
    )
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "admin"  # change the user, keep the original signature
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    resp = client.get("/reviews", headers=bearer(f"{header}.{forged}.{signature}"))

    assert resp.status_code == 401


def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200


# --- decisions and audit ---


def test_reject_without_comment_returns_422(
    client, auth_headers, waiting, review_store
):
    for body in ({"action": "reject"}, {"action": "reject", "comment": "   "}):
        resp = client.post(f"/reviews/{waiting}", json=body, headers=auth_headers)
        assert resp.status_code == 422

    detail = client.get(f"/reviews/{waiting}", headers=auth_headers).json()
    assert detail["awaiting_review"] is True
    assert review_store.history(waiting) == []


def test_decision_records_logged_in_user_as_reviewer(
    client, auth_headers, waiting, credentials, review_store
):
    # A reviewer in the body must be ignored: the reviewer comes from the login.
    resp = client.post(
        f"/reviews/{waiting}",
        json={"action": "approve", "reviewer": "someone-else"},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    [entry] = review_store.history(waiting)
    assert entry.reviewer == credentials["username"]
    assert entry.reviewer_name == credentials["name"]
    assert entry.action == "approve"
    assert entry.result_status == "posted"
    assert entry.sales_order_number == resp.json()["sales_order_number"]


def test_audit_returns_entries_in_order(client, auth_headers, waiting, po, with_line):
    still_wrong = with_line(po, 2, unit_price=Decimal("17.50"))
    edit = client.post(
        f"/reviews/{waiting}",
        json={
            "action": "edit",
            "comment": "fixed price",
            "edited_po": still_wrong.model_dump(mode="json"),
        },
        headers=auth_headers,
    )
    assert edit.json()["status"] == "awaiting_review"  # still deviates: paused again
    approve = client.post(
        f"/reviews/{waiting}", json={"action": "approve"}, headers=auth_headers
    )
    assert approve.json()["status"] == "posted"

    resp = client.get(f"/reviews/{waiting}/audit", headers=auth_headers)

    assert resp.status_code == 200
    entries = resp.json()
    assert [(e["action"], e["result_status"]) for e in entries] == [
        ("edit", "awaiting_review"),
        ("approve", "posted"),
    ]
    assert entries[0]["comment"] == "fixed price"
    assert entries[0]["po_after"] == still_wrong.model_dump(mode="json")
    assert entries[1]["po_after"] is None
    assert entries[0]["decided_at"] <= entries[1]["decided_at"]


def test_audit_needs_token(client, waiting):
    assert client.get(f"/reviews/{waiting}/audit").status_code == 401
