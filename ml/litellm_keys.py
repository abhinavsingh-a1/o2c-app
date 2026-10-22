"""Create one LiteLLM virtual key per service, each with a monthly budget (E11 Part 3).

Usage: uv run python ml/litellm_keys.py

Uses LiteLLM's admin API with LITELLM_MASTER_KEY (environment or .env; LITELLM_BASE_URL
defaults to http://localhost:4000). For every service in SERVICES it looks up the key by
key_alias (= the service name):
  - missing: creates it with max_budget (USD) per BUDGET_DURATION and prints it ONCE as
    LITELLM_KEY_<SERVICE>=sk-... for .env; LiteLLM stores only a hash, so a lost key is
    replaced by deleting it in LiteLLM and running this script again;
  - present: keeps it (the key is not shown again) and corrects its budget if it differs.
So the script can be run any number of times.

A service whose budget is used up is rejected by LiteLLM until the budget resets, so
a runaway loop in one service cannot spend the others' budget. docker-compose.yml passes
each service its own key (as LITELLM_MASTER_KEY, the settings field name for now);
evaluation scripts use LITELLM_KEY_EVAL (ml/litellm_common.py). ml/litellm_spend.py shows
the spend per key.
"""

import sys

import httpx

# service -> monthly budget in USD
SERVICES = {"classifier": 1.0, "extractor": 2.0, "agent": 1.0, "eval": 3.0}
BUDGET_DURATION = "1mo"


def env_name(service: str) -> str:
    return f"LITELLM_KEY_{service.upper()}"


def find_key(client: httpx.Client, alias: str) -> dict | None:
    """The key with this key_alias (full object, hashed token), or None."""
    response = client.get(
        "/key/list",
        params={"key_alias": alias, "return_full_object": "true", "size": 100},
    )
    response.raise_for_status()
    keys = [k for k in response.json().get("keys", []) if k.get("key_alias") == alias]
    if len(keys) > 1:
        raise SystemExit(f"{len(keys)} LiteLLM keys have the alias {alias!r}")
    return keys[0] if keys else None


def ensure_key(client: httpx.Client, service: str, budget: float) -> tuple[str, str]:
    """("created", key) or ("exists"/"updated", "") for the service's key."""
    existing = find_key(client, service)
    if existing is None:
        response = client.post(
            "/key/generate",
            json={
                "key_alias": service,
                "max_budget": budget,
                "budget_duration": BUDGET_DURATION,
                "metadata": {"service": service, "created_by": "ml/litellm_keys.py"},
            },
        )
        response.raise_for_status()
        return "created", response.json()["key"]
    if (
        existing.get("max_budget") != budget
        or existing.get("budget_duration") != BUDGET_DURATION
    ):
        response = client.post(
            "/key/update",
            json={
                "key": existing["token"],
                "max_budget": budget,
                "budget_duration": BUDGET_DURATION,
            },
        )
        response.raise_for_status()
        return "updated", ""
    return "exists", ""


def main(client: httpx.Client | None = None) -> int:
    from litellm_common import admin_client

    client = client or admin_client()
    new_keys = []
    for service, budget in SERVICES.items():
        status, key = ensure_key(client, service, budget)
        print(
            f"{service:<11} {status:<8} max_budget ${budget:.2f} per {BUDGET_DURATION}"
        )
        if key:
            new_keys.append(f"{env_name(service)}={key}")
    if new_keys:
        print("\nAdd to .env (shown only this once; LiteLLM keeps only a hash):")
        print("\n".join(new_keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
