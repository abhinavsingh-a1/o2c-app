import sys
from pathlib import Path

import pytest

# Make ml/ scripts (eval_*, train_*), ml/pipelines/ and poc/ modules (schema,
# generate_pos) importable.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "pipelines"))
sys.path.insert(0, str(ROOT / "ml"))
sys.path.insert(0, str(ROOT / "poc"))


@pytest.fixture(autouse=True)
def no_eval_key(monkeypatch):
    """Tests never pick up a real LITELLM_KEY_EVAL from .env (ml/litellm_common.py)."""
    import litellm_common

    monkeypatch.setattr(litellm_common, "eval_key", lambda: None)
