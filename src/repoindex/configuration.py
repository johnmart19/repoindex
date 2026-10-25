"""Workspace configuration, safe destinations and component selection."""
import json
from pathlib import Path
import re


def contained(root, relative):
    root = Path(root).resolve()
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Expected a relative path below " + str(root))
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("Path escapes its owning directory")
    return path


def config(root):
    root = Path(root).resolve()
    data = json.loads((root / "repoindex.json").read_text())
    trees = data["trees"]
    if not isinstance(trees, list) or not trees or not all(isinstance(name, str) for name in trees):
        raise ValueError("trees must be a nonempty list of source directory names")
    if len(trees) != len(set(trees)):
        raise ValueError("trees must be unique")
    for name in trees:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Invalid tree name")
        contained(root, name)
        contained(root, "index/" + name)
    return data


def selected(root, names=None):
    data = config(root)
    result = names or data["trees"]
    if len(result) != len(set(result)) or any(x not in data["trees"] for x in result):
        raise ValueError("Unknown or repeated tree selection")
    return result

def find_workspace(start=None):
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current,) + tuple(current.parents):
        if (candidate / "repoindex.json").is_file():
            return candidate
    raise ValueError("No repoindex.json found; specify --workspace PATH")


def scopes(root, data):
    result = data.get("scopes")
    legacy = root / "config/search-scopes.json"
    if result is None and legacy.is_file():
        result = json.loads(legacy.read_text(encoding="utf-8"))
    result = {} if result is None else result
    if not isinstance(result, dict):
        raise ValueError("scopes must map names to workspace-relative path lists")
    for name, paths in result.items():
        if not isinstance(name, str) or not name or not isinstance(paths, list):
            raise ValueError("Invalid scope definition")
        for path in paths:
            if not isinstance(path, str) or path.split("/")[0] not in data["trees"]:
                raise ValueError("Scope paths must start with a configured tree")
            contained(root, path)
    return result


def selected_paths(root, data, names, scope=None, prefixes=None):
    if scope and prefixes is not None:
        raise ValueError("Choose --scope or --path-prefix, not both")
    if scope:
        definitions = scopes(root, data)
        if scope not in definitions:
            raise ValueError("Unknown scope: " + scope)
        paths = definitions[scope]
    else:
        paths = prefixes if prefixes is not None else names
    cleaned = []
    for path in paths:
        path = path.replace("\\", "/").rstrip("/")
        contained(root, path)
        if any(path == name or path.startswith(name + "/") for name in names):
            cleaned.append(path)
    return cleaned
