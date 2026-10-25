#!/usr/bin/env python3
"""Compatibility launcher/facade. Implementation lives in src/repoindex/."""
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parent / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))

# Also support callers importing this file as the old top-level module.
if __name__ == "repoindex":
    __path__ = [str(_SOURCE / "repoindex")]
    __package__ = "repoindex"
from repoindex._compat import EXPORTS, resolve
from repoindex.cli import main

__all__ = list(EXPORTS)
__version__ = resolve("VERSION")


def __getattr__(name):
    return resolve(name)


if __name__ == "__main__":
    raise SystemExit(main())
