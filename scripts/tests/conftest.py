import sys
from pathlib import Path

# scripts/ holds standalone scripts, not a package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
