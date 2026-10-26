"""End-to-end checks of a deployed environment for the promotion pipelines (ADR-018).

Usage:
  e2e_send_and_check.py health <url> [<url> ...] [--timeout S]
  e2e_send_and_check.py order --intake-url URL --metrics-url URL --pdf PATH --run-id ID
                              [--expect posted,awaiting_review] [--timeout S]

health  every URL answers 200 (retried until --timeout). Needs only httpx:
        uv run --frozen --only-dev python scripts/e2e_send_and_check.py health ...
order   sends a PDF to intake (signed with INTAKE_HMAC_SECRET) and waits until the
        environment's agent has decided the order: its review_queue row (EVENTS_DATABASE_URL)
        reaches an --expect status and the agent's o2c_orders_total{decided_by="agent"}
        (GET --metrics-url) went up. A marker with --run-id is appended to the PDF (after
        %%EOF, which PDF readers ignore), so every run gets a new idempotency key and is
        processed again instead of being skipped as a duplicate. Needs the workspace:
        uv run --frozen --all-packages python scripts/e2e_send_and_check.py order ...

INTAKE_HMAC_SECRET and EVENTS_DATABASE_URL come from the environment or ./.env (values
are never printed). Prints one PASS/FAIL line per check; exit 1 on any failure.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
ORDERS_METRIC = "o2c_orders_total"
FINAL_FAILURES = {"failed", "rejected"}
_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


# --- parsing --------------------------------------------------------------------------


def parse_labels(text: str) -> dict[str, str]:
    """'a="1",b="x\\"y"' -> {"a": "1", "b": 'x"y'} (Prometheus text format)."""
    return {
        k: v.replace(r"\"", '"').replace(r"\n", "\n").replace("\\\\", "\\")
        for k, v in _LABEL.findall(text)
    }


def metric_sum(
    exposition: str, name: str, labels: dict[str, str] | None = None
) -> float:
    """Sum of the samples of `name` whose labels include `labels`. Raises ValueError if
    the metric is not exposed at all (a wrong URL must not look like zero orders)."""
    total, found = 0.0, False
    for line in exposition.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match or match.group(1) != name:
            continue
        found = True
        sample = parse_labels(match.group(2) or "")
        if all(sample.get(k) == v for k, v in (labels or {}).items()):
            total += float(match.group(3))
    if not found:
        raise ValueError(f"metric {name} not found")
    return total


def libpq_url(database_url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://...) -> libpq URL for psycopg.connect."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", database_url)


def parse_expect(text: str) -> set[str]:
    statuses = {s.strip() for s in text.split(",") if s.strip()}
    if not statuses or statuses & FINAL_FAILURES:
        raise argparse.ArgumentTypeError(
            f"expected statuses must not be empty or in {FINAL_FAILURES}"
        )
    return statuses


# --- request --------------------------------------------------------------------------


def unique_pdf(content: bytes, run_id: str) -> bytes:
    """The PDF with a comment line after %%EOF: same document, new idempotency key."""
    if not content.startswith(b"%PDF-"):
        raise ValueError("not a PDF")
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"run id must match {RUN_ID.pattern}: {run_id!r}")
    return content.rstrip(b"\r\n") + f"\n% o2c-e2e {run_id}\n".encode()


def sign(secret: str, body: bytes) -> str:
    """X-O2C-Signature as intake.app.sign computes it (hex HMAC-SHA256 of the body)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def intake_body(pdf: bytes, filename: str, run_id: str) -> bytes:
    return json.dumps(
        {
            "source": "upload",
            "filename": filename,
            "sender": f"e2e+{run_id}@example.com",
            "content_base64": base64.b64encode(pdf).decode(),
        }
    ).encode()


def idempotency_key(pdf: bytes) -> str:
    """The key intake derives for source "upload" (o2c_common.ids.idempotency_key)."""
    return hashlib.sha256(b"upload:" + pdf).hexdigest()


# --- checks ---------------------------------------------------------------------------


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value and Path(".env").is_file():
        from dotenv import dotenv_values

        value = dotenv_values(".env").get(name)
    if not value:
        raise SystemExit(f"FAIL {name} is not set (environment or .env)")
    return value


def wait_for[T](
    probe: Callable[[], T | None],
    timeout: float,
    interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T | None:
    """probe() until it returns something other than None, at most `timeout` seconds."""
    deadline = clock() + timeout
    while True:
        result = probe()
        if result is not None or clock() >= deadline:
            return result
        sleep(interval)


def check_health(client: httpx.Client, urls: list[str], timeout: float) -> bool:
    ok = True
    for url in urls:

        def probe(url=url) -> int | None:
            try:
                status = client.get(url).status_code
            except httpx.HTTPError:
                return None
            return status if status == 200 else None

        status = wait_for(probe, timeout, interval=3.0)
        print(f"{'PASS' if status == 200 else 'FAIL'} health {url}")
        ok = ok and status == 200
    return ok


def agent_orders(client: httpx.Client, metrics_url: str) -> float:
    response = client.get(metrics_url)
    response.raise_for_status()
    return metric_sum(response.text, ORDERS_METRIC, {"decided_by": "agent"})


def review_status(database_url: str, key: str) -> str | None:
    import psycopg

    with psycopg.connect(libpq_url(database_url), connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT status FROM review_queue WHERE idempotency_key = %s", (key,)
        ).fetchone()
    return row[0] if row else None


def check_order(client: httpx.Client, args: argparse.Namespace) -> bool:
    secret, database_url = setting("INTAKE_HMAC_SECRET"), setting("EVENTS_DATABASE_URL")
    pdf = unique_pdf(args.pdf.read_bytes(), args.run_id)
    key = idempotency_key(pdf)
    before = agent_orders(client, args.metrics_url)

    body = intake_body(pdf, args.pdf.name, args.run_id)
    response = client.post(
        args.intake_url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-O2C-Signature": sign(secret, body),
        },
    )
    accepted = (
        response.status_code == 202 and response.json().get("idempotency_key") == key
    )
    print(
        f"{'PASS' if accepted else 'FAIL'} intake accepted {args.pdf.name} "
        f"(HTTP {response.status_code}, key {key[:12]})"
    )
    if not accepted:
        return False

    def decided() -> str | None:
        status = review_status(database_url, key)
        return status if status in args.expect | FINAL_FAILURES else None

    status = wait_for(decided, args.timeout)
    ok = status in args.expect
    print(
        f"{'PASS' if ok else 'FAIL'} agent decided the order: "
        f"{status or 'no decision'} (expected {', '.join(sorted(args.expect))})"
    )
    if not ok:
        return False

    after = wait_for(
        lambda: n if (n := agent_orders(client, args.metrics_url)) > before else None,
        timeout=60,
    )
    counted = after is not None
    print(
        f'{"PASS" if counted else "FAIL"} {ORDERS_METRIC}{{decided_by="agent"}} '
        f"{before:g} -> {after if counted else before:g} at {args.metrics_url}"
    )
    return counted


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("health")
    health.add_argument("urls", nargs="+")
    health.add_argument("--timeout", type=float, default=120)
    order = sub.add_parser("order")
    order.add_argument("--intake-url", required=True)
    order.add_argument("--metrics-url", required=True)
    order.add_argument("--pdf", type=Path, required=True)
    order.add_argument("--run-id", required=True)
    order.add_argument(
        "--expect", type=parse_expect, default=parse_expect("posted,awaiting_review")
    )
    order.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)

    with client or httpx.Client(timeout=30) as http:
        try:
            if args.command == "health":
                ok = check_health(http, args.urls, args.timeout)
            else:
                ok = check_order(http, args)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            print(f"FAIL {type(exc).__name__}: {exc}")
            ok = False
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
