"""Standalone workspace CLI: bounded queries, live text and explicit maintenance."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

import workspace_index as workspace

QUERY_MODES = {"files", "symbol", "impl", "source", "text"}
ACTIONS = sorted(QUERY_MODES | {"search", "scopes", "status", "restore", "guide",
                              "benchmark", "index", "implement", "optimize", "verify"})


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
            workspace.contained(root, path)
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
        workspace.contained(root, path)
        if any(path == name or path.startswith(name + "/") for name in names):
            cleaned.append(path)
    return cleaned


def status(root, names):
    for tree in names:
        data = workspace.manifest(root, tree)
        meta = data["metadata"]
        yield {"tree": tree, "files": int(meta["file_count"]),
               "header_symbols": int(meta["symbol_count"]),
               "implementation_files": int(meta.get("implementation_count", "0")),
               "implementation_bytes": int(meta.get("implementation_bytes", "0")),
               "implementation_decode_replacements": int(meta.get("implementation_decode_errors", "0")),
               "implementation_normalized_files": int(meta.get("implementation_normalized_files", "0")),
               "scan_errors": int(meta["error_count"]), "updated": meta["updated"],
               "chunks": len(data["parts"]), "database_bytes": data["database_bytes"],
               "compressed_bytes": sum(part["bytes"] for part in data["parts"]),
               "search_backend": meta.get("search_backend", "legacy"),
               "cache_ready": bool(workspace.cached_db(root, tree, data)),
               "live_tree_verified": False}


def live_text(root, query, prefixes, regex=False, width=400):
    missing = [path for path in prefixes if not (root / path).exists()]
    if missing:
        raise ValueError("Live source unavailable; use impl/source or materialize: " + ", ".join(missing))
    cmd = ["rg", "--no-config", "--hidden", "--no-ignore", "--line-number",
           "--no-heading", "--color", "never", "--max-columns", str(width),
           "--max-columns-preview"]
    for name in {".git", ".repo", ".hg", ".svn", ".cache"} | set(workspace.config(root).get("ignore", [])):
        cmd += ["--glob", "!**/" + name + "/**"]
    ignore = root / ".rgignore"
    if ignore.is_file():
        cmd += ["--ignore-file", str(ignore)]
    if not regex:
        cmd.append("--fixed-strings")
    cmd += ["--", query] + list(prefixes)
    with subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, text=True,
                          errors="replace") as process:
        try:
            for line in process.stdout:
                yield {"live": line.rstrip("\n")}
            code = process.wait()
            if code not in (0, 1):
                raise ValueError("ripgrep failed (exit %d)" % code)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()


def write_guide(root, output=None, replace=False):
    """Generate only the owned block; never scan sources or overwrite curated prose."""
    import repoindex
    root = Path(root).resolve()
    data = workspace.config(root)
    output = output or data.get("guide")
    if not output:
        raise ValueError("Set guide in repoindex.json or pass --out RELATIVE_PATH")
    if (root / output).is_symlink():
        raise ValueError("Refusing a symlink guide output")
    target = workspace.contained(root, output)
    command = data.get("command", "python3 repoindex.py workspace")
    lines = ["# Index snapshot", "",
             "Stored provenance only; not a live freshness check. No source scan is needed to query.", "",
             "| Tree | Files | Header symbols | Implementations | SQLite MiB | Bundle MiB | Chunks |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for tree in data["trees"]:
        if not (root / "index" / tree / "manifest.json").is_file():
            lines.append("| %s | not built | — | — | — | — | — |" % tree)
            continue
        item = workspace.manifest(root, tree)
        meta = item["metadata"]
        lines.append("| %s | %s | %s | %s | %.1f | %.1f | %d |" % (
            tree, meta["file_count"], meta["symbol_count"],
            meta.get("implementation_count", "not indexed"), item["database_bytes"] / 1024**2,
            sum(part["bytes"] for part in item["parts"]) / 1024**2, len(item["parts"])))
    lines += ["", "All configured trees are selected by default; narrow with `--tree` or `--scope`.",
              "Queries restore verified caches once; they never rebuild source indexes.", "",
              "Use `" + command + " files QUERY` to locate paths, `impl QUERY` for bodies,",
              "and `source PATH --line N --limit 20` for small indexed ranges.",
              "Source code is a snapshot; header extraction is heuristic and body search is literal.",
              "Raw chunks are <=32 MiB, compressed chunks <=50 MiB. Refresh only after source changes.",
              ""]
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    text = repoindex.merge_guide(existing, "\n".join(lines), replace=replace)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(text)
    try:
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"guide": target.relative_to(root).as_posix()}


def benchmark(root, names, data, samples=5, output=None, compare=None, budget=None):
    if samples < 3:
        raise ValueError("Use at least three benchmark samples")
    cases = data.get("benchmarks", [])
    if not cases:
        raise ValueError("No benchmarks configured in repoindex.json")
    previous = {}
    if compare:
        for row in json.loads(Path(compare).read_text(encoding="utf-8")):
            previous[json.dumps([row["mode"], row.get("query", row.get("pattern")), selected_paths(root, data, names, None, row.get("prefixes"))])] = row
    for tree in names:
        workspace.ensure_db(root, tree)
    results = []
    exceeded = False
    for case in cases:
        mode, query = case["mode"], case["query"]
        if mode not in {"files", "symbol", "impl"}:
            raise ValueError("Benchmark mode must be files, symbol or impl")
        prefixes = selected_paths(root, data, names, case.get("scope"), case.get("prefixes"))
        timings = []
        for _ in range(samples):
            with contextlib.redirect_stderr(io.StringIO()):
                started = time.perf_counter()
                rows = list(workspace.query(root, names, mode, query, case.get("limit", 20), prefixes)) if prefixes else []
                timings.append((time.perf_counter() - started) * 1000)
        median = statistics.median(timings)
        item = {"mode": mode, "query": query, "prefixes": prefixes,
                "median_ms": round(median, 3), "samples_ms": [round(value, 3) for value in timings],
                "rows": len(rows)}
        old = previous.get(json.dumps([mode, query, prefixes]))
        if old:
            if old["rows"] != len(rows):
                raise ValueError("Benchmark result count changed: " + query)
            item.update(before_ms=old["median_ms"], speedup=round(old["median_ms"] / median, 2))
        exceeded |= budget is not None and median > budget
        results.append(item)
    if output:
        Path(output).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results, exceeded


def format_record(row, width=400):
    if "live" in row:
        return row["live"]
    if "median_ms" in row:
        return "%7.3f ms  %-6s %s  (%s rows)" % (row["median_ms"], row["mode"], row["query"][:width], row["rows"])
    if "path" in row:
        path = row["path"]
        if "start_line" in row:
            numbered = ["%d | %s" % (row["start_line"] + index, line) for index, line in enumerate(row["text"].split("\n"))]
            return path + ":%d-%d\n" % (row["start_line"], row["end_line"]) + "\n".join(numbered)
        if "line" in row:
            result = "%s:%s: %s" % (path, row["line"], row["text"])
            if "context" in row:
                result += "\n" + row["context"]
            return result
        if "declaration" in row:
            declaration = row["declaration"]
            return "%s [%s] %s" % (path, row["kind"], declaration[:width] + ("…" if len(declaration) > width else ""))
        return path
    if "files" in row and "cache_ready" in row:
        return "%s: %s files, %s implementations, %s symbols; cache=%s, backend=%s, errors=%s" % (
            row["tree"], row["files"], row["implementation_files"], row["header_symbols"],
            "ready" if row["cache_ready"] else "cold", row["search_backend"], row["scan_errors"])
    if "scope" in row:
        return row["scope"] + ": " + ", ".join(row["paths"])
    return json.dumps(row, ensure_ascii=False)


def emit(rows, json_output=False, budget=12000, width=400):
    """Stop the producer when stdout reaches its budget; JSON records stay valid."""
    used = emitted = 0
    iterator = iter(rows)
    try:
        for row in iterator:
            if json_output:
                row = dict(row)
                if "declaration" in row and len(row["declaration"]) > width:
                    row.update(declaration=row["declaration"][:width], truncated=True)
                text = json.dumps(row, ensure_ascii=False)
            else:
                text = format_record(row, width)
            encoded = (text + "\n").encode("utf-8")
            if used + len(encoded) > budget:
                if not json_output:
                    available = max(0, budget - used)
                    clipped = encoded[:available].decode("utf-8", errors="ignore")
                    print(clipped, end="")
                print("Output budget reached; narrow the query/range or raise --max-output-bytes.", file=sys.stderr)
                return 0 if emitted or not json_output else 3
            print(text)
            used += len(encoded)
            emitted += 1
        return 0
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("query", nargs="?")
    parser.add_argument("--workspace", help="Directory containing repoindex.json; default discover from cwd")
    parser.add_argument("--tree", action="append", help="Select one configured tree (repeatable); default all")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--scope", help="Named paths from repoindex.json")
    selection.add_argument("--path-prefix", action="append")
    parser.add_argument("--limit", type=int, default=20, help="Global results/lines (default 20)")
    parser.add_argument("--max-chars", type=int, default=400)
    parser.add_argument("--max-output-bytes", type=int, default=12000)
    parser.add_argument("--json", action="store_true", help="JSON Lines instead of compact human-readable output")
    parser.add_argument("--context", type=int, default=0)
    parser.add_argument("--line", type=int, default=1)
    parser.add_argument("--regex", action="store_true", help="Live text only")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--chunk-mib", type=int, default=32)
    parser.add_argument("--live", action="store_true", help="verify: compare live file metadata")
    parser.add_argument("--hash-implementations", action="store_true")
    parser.add_argument("--out", help="guide: workspace-relative path; benchmark: result JSON path")
    parser.add_argument("--replace", action="store_true", help="guide: explicitly replace an unmarked output")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--compare")
    parser.add_argument("--budget-ms", type=float)
    args = parser.parse_args(argv)
    try:
        root = Path(args.workspace).resolve() if args.workspace else find_workspace()
        data = workspace.config(root)
        names = workspace.selected(root, args.tree)
        action = "files" if args.action == "search" else args.action
        if args.limit < 1 or args.max_output_bytes < 128 or not 16 <= args.max_chars <= 2000:
            raise ValueError("Require positive limit, output budget >=128 and width 16..2000")
        if args.jobs < 1 or not 1 <= args.chunk_mib <= 32 or not 0 <= args.context <= 5:
            raise ValueError("Require positive jobs, chunk size 1..32 MiB and context 0..5")
        if args.regex and action != "text":
            raise ValueError("--regex is for live text only; impl uses exact literals")
        if args.scope and action not in QUERY_MODES:
            raise ValueError("--scope applies to query commands, not whole-tree maintenance")
        if action in QUERY_MODES and not args.query:
            raise ValueError("Query or source path required")
        if action not in QUERY_MODES and args.query is not None:
            raise ValueError("This command takes no query; select paths with --workspace/--tree")
        exceeded = False
        if action in QUERY_MODES:
            prefixes = selected_paths(root, data, names, args.scope, args.path_prefix)
            if not prefixes:
                return 0
            if action == "source":
                rows = workspace.read_source(root, names, args.query, args.line, args.limit, args.max_chars, prefixes)
            elif action == "text":
                def bounded_live():
                    count = 0
                    stream = live_text(root, args.query, prefixes, args.regex, args.max_chars)
                    try:
                        for row in stream:
                            if count == args.limit:
                                print("Result limit reached; narrow the query or raise --limit.", file=sys.stderr)
                                return
                            count += 1
                            yield row
                    finally:
                        stream.close()
                rows = bounded_live()
            else:
                rows = workspace.query(root, names, action, args.query, args.limit, prefixes, args.context, args.max_chars)
        elif action == "status":
            rows = status(root, names)
        elif action == "scopes":
            rows = ({"scope": name, "paths": paths} for name, paths in scopes(root, data).items())
        elif action == "guide":
            rows = [write_guide(root, args.out, args.replace)]
        elif action == "benchmark":
            rows, exceeded = benchmark(root, names, data, args.samples, args.out, args.compare, args.budget_ms)
        else:
            def maintenance():
                for tree in names:
                    if action == "index":
                        yield workspace.build(root, tree, args.jobs, args.chunk_mib * 1024**2)
                    elif action in {"implement", "optimize"}:
                        yield workspace.optimize(root, tree, implementations=action == "implement")
                    elif action == "verify":
                        yield workspace.verify(root, tree, args.live, args.hash_implementations)
                    elif action == "restore":
                        yield {"tree": tree, "cache": str(workspace.ensure_db(root, tree))}
                if action in {"index", "implement", "optimize"} and data.get("guide"):
                    write_guide(root)
            # An output cap must never cancel explicit maintenance on later trees.
            rows = list(maintenance())
        code = emit(rows, args.json, args.max_output_bytes, args.max_chars)
        if exceeded:
            print("Warm median exceeded --budget-ms.", file=sys.stderr)
            return 1
        return code
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            sys.stdout.close()
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print("repoindex: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
