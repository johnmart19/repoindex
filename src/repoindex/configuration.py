"""Workspace configuration, safe destinations and component selection."""
import json
from pathlib import Path, PurePosixPath
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
    if not isinstance(data, dict):
        raise ValueError("repoindex.json must contain an object")
    trees = data.get("trees")
    if not isinstance(trees, list) or not trees or not all(isinstance(name, str) for name in trees):
        raise ValueError("trees must be a nonempty list of source directory names")
    if len(trees) != len(set(trees)):
        raise ValueError("trees must be unique")
    for name in trees:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Invalid tree name")
        contained(root, name)
        contained(root, "index/" + name)
    ignored = data.get("ignore", [])
    if not isinstance(ignored, list) or any(not isinstance(name, str) or not name or
            name in {".", ".."} or "/" in name or "\\" in name for name in ignored):
        raise ValueError("ignore must be a list of directory names")
    extensions = data.get("implementation_extensions", [".cpp", ".cc"])
    if not isinstance(extensions, list) or not extensions or any(
            not isinstance(ext, str) or not re.fullmatch(r"\.[A-Za-z0-9+]+", ext) for ext in extensions):
        raise ValueError("implementation_extensions must be a nonempty list of suffixes")
    for name in ("guide", "command"):
        if name in data and data[name] is not None and (not isinstance(data[name], str) or not data[name]):
            raise ValueError(name + " must be a nonempty string")
    return data


def selected(root, names=None):
    data = config(root)
    result = data["trees"] if names is None else names
    if not isinstance(result, (list, tuple)) or any(not isinstance(name, str) for name in result):
        raise ValueError("Tree selection must be a list of names")
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
    root = Path(root).resolve()
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
            path = normalized_path(root, path)
            if path.split("/")[0] not in data["trees"]:
                raise ValueError("Scope paths must start with a configured tree")
    return result


def normalized_path(root, path):
    if not isinstance(path, str) or not path:
        raise ValueError("Expected a nonempty workspace-relative path")
    path = path.replace("\\", "/")
    contained(root, path)
    return PurePosixPath(path).as_posix().rstrip("/")


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
    if not isinstance(paths, (list, tuple)):
        raise ValueError("Path selection must be a list")
    cleaned = []
    for path in paths:
        path = normalized_path(root, path)
        if path.split("/")[0] not in data["trees"]:
            raise ValueError("Path must start with a configured tree")
        if not any(path == name or path.startswith(name + "/") for name in names):
            continue
        if any(path == other or path.startswith(other + "/") for other in cleaned):
            continue
        cleaned = [other for other in cleaned if not other.startswith(path + "/")]
        cleaned.append(path)
    return cleaned
