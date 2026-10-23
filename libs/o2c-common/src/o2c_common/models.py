"""Models baked into service images (ADR-016).

The release workflow pulls the model artifacts pinned in models.lock.json (published by
ml/publish_models.py) into /app/models/<registered model name>: an MLflow model
directory with its MLmodel file. Services load a baked model first and fall back to
MLflow (@champion) only if no baked model exists, e.g. in local Compose builds.
"""

import os
from pathlib import Path

MODELS_DIR_ENV = "O2C_MODELS_DIR"
DEFAULT_MODELS_DIR = Path("/app/models")


def models_dir() -> Path:
    """Where baked models live: $O2C_MODELS_DIR, default /app/models."""
    return Path(os.environ.get(MODELS_DIR_ENV) or DEFAULT_MODELS_DIR)


def baked_model(name: str) -> Path | None:
    """The baked MLflow model directory of `name`, or None if the image has none."""
    path = models_dir() / name
    return path if (path / "MLmodel").is_file() else None
