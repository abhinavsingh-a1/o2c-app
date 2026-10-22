"""Langfuse client, git version and trace ids for the evaluation scripts (ADR-010).

LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY come from the environment or .env,
LANGFUSE_HOST defaults to http://localhost:3000 (the local stack). The SDK is langfuse 2.x:
the newest that fits next to mlflow-skinny 2.17 (protobuf < 6); the Langfuse 3 server
still serves its API.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "http://localhost:3000"


def setting(name: str, default: str | None = None) -> str | None:
    return (
        os.environ.get(name) or dotenv_values(REPO_ROOT / ".env").get(name) or default
    )


def make_langfuse():
    from langfuse import Langfuse

    public_key, secret_key = (
        setting("LANGFUSE_PUBLIC_KEY"),
        setting("LANGFUSE_SECRET_KEY"),
    )
    if not public_key or not secret_key:
        raise SystemExit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set")
    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=setting("LANGFUSE_HOST", DEFAULT_HOST),
    )


def git_short_sha(cwd: Path = REPO_ROOT) -> str:
    """Short sha of HEAD; "nogit" outside a repository."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "nogit"


def trace_id(*parts: str) -> str:
    """Deterministic 32-hex Langfuse trace id: the same parts give the same trace."""
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:32]
