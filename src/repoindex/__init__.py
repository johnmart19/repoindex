"""repoindex package. Prefer workspace.query/read_source for new Python callers."""
from ._version import VERSION
from ._compat import EXPORTS, resolve

__version__ = VERSION
__all__ = list(EXPORTS)


def __getattr__(name):
    return resolve(name)
