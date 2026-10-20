import getpass
import hashlib
import hmac
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from fastapi import HTTPException
from pydantic import BaseModel


class User(BaseModel):
    username: str
    name: str


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1
    )
    return hmac.compare_digest(candidate.hex(), digest)


class Authenticator:
    def __init__(self, users_file: Path, secret: str, hours: int = 8):
        self._users_file, self._secret, self._hours = users_file, secret, hours

    def _users(self) -> dict:
        if not self._users_file.is_file():
            return {}
        return json.loads(self._users_file.read_text(encoding="utf-8"))

    def login(self, username: str, password: str) -> LoginResponse | None:
        entry = self._users().get(username)
        if not entry or not verify_password(password, entry["password_hash"]):
            return None
        user = User(username=username, name=entry["name"])
        claims = {
            "sub": username,
            "name": user.name,
            "exp": datetime.now(UTC) + timedelta(hours=self._hours),
        }
        return LoginResponse(
            access_token=jwt.encode(claims, self._secret, algorithm="HS256"), user=user
        )

    def verify(self, token: str) -> User:
        try:
            claims = jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(401, "invalid or expired login") from exc
        return User(username=claims["sub"], name=claims["name"])


def add_user(users_file: Path, username: str, name: str) -> None:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
    else:  # Git Bash cannot hide input; the password is visible while typing
        password = input("Password (visible): ")
    if len(password) < 8:
        sys.exit("password must have at least 8 characters")
    users = (
        json.loads(users_file.read_text(encoding="utf-8"))
        if users_file.is_file()
        else {}
    )
    users[username] = {"name": name, "password_hash": hash_password(password)}
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")
    print(f"saved user {username} ({name}) to {users_file}")


if __name__ == "__main__":
    from agent.settings import Settings

    if len(sys.argv) != 4 or sys.argv[1] != "add-user":
        sys.exit('usage: python -m agent.auth add-user <username> "<Full Name>"')
    add_user(Settings().auth_users_file, sys.argv[2], sys.argv[3])
