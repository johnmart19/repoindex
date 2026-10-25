"""Run the complete source directory without installation."""
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parent / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))

from repoindex.cli import main

raise SystemExit(main())
