"""Application entry point; implementations do not depend on this module."""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "workspace":
        from .workspace_cli import main as workspace_main
        return workspace_main(argv[1:])
    from .legacy_cli import main as legacy_main
    return legacy_main(argv)
