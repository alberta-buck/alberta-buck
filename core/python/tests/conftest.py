import sys
from pathlib import Path

# Make the adjacent buck_core package importable when running the suite
# from the repo root (python -m pytest core/python/tests).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
