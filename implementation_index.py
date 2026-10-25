"""Compatibility import; use repoindex.implementations in new code."""
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parent / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))

from repoindex import implementations as _implementation

sys.modules[__name__] = _implementation
