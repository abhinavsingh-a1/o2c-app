import sys
from pathlib import Path

# Make poc/ modules (schema, generate_pos) importable from the tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
