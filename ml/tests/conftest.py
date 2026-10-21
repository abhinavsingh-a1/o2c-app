import sys
from pathlib import Path

# Make ml/ scripts (eval_*, train_*), ml/pipelines/ and poc/ modules (schema,
# generate_pos) importable.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "pipelines"))
sys.path.insert(0, str(ROOT / "ml"))
sys.path.insert(0, str(ROOT / "poc"))
