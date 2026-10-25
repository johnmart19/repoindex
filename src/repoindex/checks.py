"""Opt-in documentation checks shared by the tool and source workspaces."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def prose(text):
    return re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.S)


def documentation_errors(root, budgets, paths=None):
    root = Path(root).resolve()
    errors = []
    for name, maximum in budgets.items():
        path = root / name
        if not path.is_file() or path.stat().st_size > maximum:
            errors.append("Missing or oversized entry point: " + name)
    paths = paths if paths is not None else list(root.glob("*.md")) + list((root / "docs").glob("*.md"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if text.startswith("\ufeff") or text.count("```") % 2:
            errors.append("BOM or unmatched code fence: " + str(path.relative_to(root)))
        for raw in re.findall(r"\]\(([^)]+)\)", prose(text)):
            link = urlsplit(raw)
            if link.scheme or raw.startswith("//"):
                continue
            target = (path.parent / unquote(link.path)).resolve() if link.path else path
            if root not in target.parents or not target.is_file():
                errors.append("Broken local link: " + str(path.relative_to(root)) + " -> " + raw)
    return errors
