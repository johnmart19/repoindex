"""Compatibility import; use repoindex.workspace in new code."""
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parent / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))

from repoindex import workspace as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
