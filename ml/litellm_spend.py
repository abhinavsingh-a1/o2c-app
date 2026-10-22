"""Show LiteLLM spend per virtual key (E11 Part 3).

Usage: uv run python ml/litellm_spend.py

Lists every key with a key_alias (the service keys of ml/litellm_keys.py and any other)
through LiteLLM's admin API with LITELLM_MASTER_KEY: spend in USD in the current budget
period, max_budget, the share used and when the budget resets. Calls made with the
master key itself are not attributed to a service key and do not appear here.
"""

import sys

import httpx

COLUMNS = ["key", "spend", "budget", "used", "period", "resets"]


def list_keys(client: httpx.Client) -> list[dict]:
    keys, page = [], 1
    while True:
        response = client.get(
            "/key/list",
            params={"return_full_object": "true", "page": page, "size": 100},
        )
        response.raise_for_status()
        data = response.json()
        keys += data.get("keys", [])
        if page >= (data.get("total_pages") or 1):
            return [k for k in keys if k.get("key_alias")]
        page += 1


def spend_rows(keys: list[dict]) -> list[list[str]]:
    rows = []
    for k in sorted(keys, key=lambda k: k["key_alias"]):
        spend, budget = k.get("spend") or 0.0, k.get("max_budget")
        rows.append(
            [
                k["key_alias"],
                f"${spend:.4f}",
                "none" if budget is None else f"${budget:.2f}",
                "" if not budget else f"{spend / budget:.1%}",
                k.get("budget_duration") or "",
                (k.get("budget_reset_at") or "")[:10],
            ]
        )
    return rows


def format_table(rows: list[list[str]]) -> str:
    table = [COLUMNS, *rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(COLUMNS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main(client: httpx.Client | None = None) -> int:
    from litellm_common import admin_client

    keys = list_keys(client or admin_client())
    rows = spend_rows(keys)
    print(format_table(rows))
    total = sum(k.get("spend") or 0.0 for k in keys)
    print(f"\ntotal over {len(keys)} keys: ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
