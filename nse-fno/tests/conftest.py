"""Makes `fno` importable. technical.py from the NSE dashboard is OPTIONAL: if it is beside fno/ (or in the folder named
by FNO_TECHNICAL_DIR) the signals use it, otherwise they use the bundled copy in fno/_technical.py."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), os.getenv("FNO_TECHNICAL_DIR", "")):
    if p and p not in sys.path:
        sys.path.insert(0, p)
