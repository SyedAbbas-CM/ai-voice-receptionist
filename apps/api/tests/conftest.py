import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, API_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
