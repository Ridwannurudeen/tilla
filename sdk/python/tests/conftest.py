"""Make ``tilla_sdk`` importable when the suite runs from source without an
editable install (CI installs the package; local runs may not)."""

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
