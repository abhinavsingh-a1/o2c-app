"""LiteLLM config for CI (ADR-015): the repo's config without what CI does not have.

Usage: uv run python ml/ci_litellm_config.py <output path>

Keeps the model list (the same aliases and models, so the eval measures what runs
locally), drop_params and the router fallbacks. Removes:
  - general_settings.database_url: no LiteLLM database in CI (the master key works
    without one; virtual keys and spend logs are not needed);
  - the langfuse and prometheus callbacks: no Langfuse in CI, nobody scrapes metrics.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "local" / "litellm" / "config.yaml"
DROPPED_SETTINGS = (
    "success_callback",
    "failure_callback",
    "callbacks",
    "require_auth_for_metrics_endpoint",
)


def ci_config(config: dict) -> dict:
    config = dict(config)
    general = dict(config.get("general_settings") or {})
    general.pop("database_url", None)
    config["general_settings"] = general
    settings = {
        k: v
        for k, v in (config.get("litellm_settings") or {}).items()
        if k not in DROPPED_SETTINGS
    }
    config["litellm_settings"] = settings
    return config


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit(__doc__)
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    Path(args[0]).write_text(
        yaml.safe_dump(ci_config(config), sort_keys=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
