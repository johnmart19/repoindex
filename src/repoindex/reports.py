"""Explicit symbol and header inventory exports."""
from contextlib import closing
import fnmatch
import os
import re
import sys
from collections import defaultdict
from datetime import date
from .database import human_size, open_db_required, posix_rel, resolve_db
from .headers import parse_header


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def symbol_query(args, extra_conds=None, extra_params=None):
    sql = "SELECT name, kind, decl, path FROM vsymbols"
    conds, params = [], []
    if args.kind:
        conds.append("kind = ?")
        params.append(args.kind)
    if args.pattern:
        conds.append("name GLOB ?")
        params.append(args.pattern)
    if getattr(args, "path_prefix", None):
        conds.append("path LIKE ? ESCAPE '\\'")
        params.append(like_escape(args.path_prefix) + "%")
    if extra_conds:
        conds.extend(extra_conds)
        params.extend(extra_params or [])
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY name COLLATE NOCASE, path"
    return sql, params


GUARD_DECL_RE = re.compile(r"^#define ([A-Za-z_]\w*)\s*$")


GUARD_NAME_RE = re.compile(r"_(H|HPP|HH|HXX)_?$")


def is_include_guard(name, decl):
    """Heuristic: ALL_CAPS macro named like its file (FOO_H), no value."""
    m = GUARD_DECL_RE.match(decl)
    return bool(m and m.group(1) == name and name.isupper()
                and GUARD_NAME_RE.search(name))


def keep_row(args, name, kind, decl):
    """Dump-time token savers. The index always keeps everything; these
    filters only affect dumps."""
    if getattr(args, "no_guards", False) and kind == "define" \
            and is_include_guard(name, decl):
        return False
    if getattr(args, "defs_only", False) \
            and kind in ("class", "struct", "union") \
            and decl.rstrip().endswith(";"):
        return False
    return True


def write_symbols_md(f, title, cursor, note, args=None):
    f.write("# %s\n\n%s\n\n" % (title, note))
    f.write("| Symbol | Kind | Declaration | Header |\n"
            "|--------|------|-------------|--------|\n")
    n = 0
    for name, kind, decl, path in cursor:
        if args is not None and not keep_row(args, name, kind, decl):
            continue
        f.write("| `%s` | %s | `%s` | `%s` |\n" %
                (name, kind, decl.replace("|", "\\|"), path))
        n += 1
    return n


def write_symbols_dedup(f, cursor, args):
    """Merge identical (name, kind, decl) rows; every exposing header is
    listed on the merged row."""
    f.write("# Symbol index (deduplicated)\n\nIdentical declarations across "
            "headers are merged; every exposing header is listed.\n\n"
            "| Symbol | Kind | Declaration | Headers |\n"
            "|--------|------|-------------|---------|\n")
    n = 0
    cur_key, cur_paths = None, []

    def flush_row():
        nonlocal n
        if cur_key is None:
            return
        name, kind, decl = cur_key
        if len(cur_paths) == 1:
            where = "`%s`" % cur_paths[0]
        else:
            where = "%d headers: %s" % (len(cur_paths), "; ".join(cur_paths))
        f.write("| `%s` | %s | `%s` | %s |\n" %
                (name, kind, decl.replace("|", "\\|"), where))
        n += 1

    for name, kind, decl, path in cursor:
        if not keep_row(args, name, kind, decl):
            continue
        key = (name, kind, decl)
        if key != cur_key:
            flush_row()
            cur_key, cur_paths = key, []
        cur_paths.append(path)
    flush_row()
    return n


def symbols_out_dir(db, args):
    """Adaptive per-component dump: split recursively until no file holds
    more than --max-per-file symbols. Single streamed pass over the symbol
    table; memory stays flat regardless of tree size."""
    if getattr(args, "path_prefix", None):
        raise ValueError("--path-prefix does not combine with --out-dir; use "
                 "--out with --path-prefix instead.")
    if getattr(args, "dedup", False):
        raise ValueError("--dedup does not combine with --out-dir (merging spans "
                 "components); use --out.")
    max_per = max(1, args.max_per_file)
    os.makedirs(args.out_dir, exist_ok=True)

    w, p = [], []
    if args.kind:
        w.append("kind = ?")
        p.append(args.kind)
    if args.pattern:
        w.append("name GLOB ?")
        p.append(args.pattern)
    where = (" WHERE " + " AND ".join(w)) if w else ""

    dirpaths = dict(db.execute("SELECT id, path FROM dirs"))
    direct = {}
    for did, c in db.execute("SELECT dir_id, COUNT(*) FROM symbols" + where +
                             " GROUP BY dir_id", p):
        direct[dirpaths.get(did, "")] = c
    subtree = defaultdict(int)
    children = defaultdict(list)
    for dpath in dirpaths.values():
        parent = dpath.rsplit("/", 1)[0] if "/" in dpath else ""
        children[parent].append(dpath)
    for dpath, c in direct.items():
        d = dpath
        while True:
            subtree[d] += c
            if d == "":
                break
            d = d.rsplit("/", 1)[0] if "/" in d else ""

    # Leaves get one file covering their whole subtree; split nodes with
    # headers of their own additionally get a '*__direct.md' file.
    leaf_set = set()
    direct_nodes = set()

    def decide(prefix, depth):
        total = subtree.get(prefix, 0)
        if total == 0:
            return
        kids = [k for k in children.get(prefix, []) if subtree.get(k, 0)]
        if total <= max_per or not kids or depth >= 5:
            leaf_set.add(prefix)
            return
        if direct.get(prefix, 0):
            direct_nodes.add(prefix)
        for k in kids:
            decide(k, depth + 1)

    decide("", 0)

    def fname(prefix, is_direct):
        if prefix == "":
            return "_root.md" if is_direct else "_all.md"
        base = prefix.replace("/", "__")
        return base + ("__direct.md" if is_direct else ".md")

    # Split each component file by kind group: types (small, LLM-readable),
    # defines and vars (usually huge; grep material, not context material).
    KIND_GROUP = {"class": "types", "struct": "types", "enum": "types",
                  "union": "types", "define": "defines", "var": "vars"}
    GROUP_TITLE = {
        "types": "type declarations (class/struct/enum/union)",
        "defines": "macros (#define)",
        "vars": "global variables",
    }
    handles, counts = {}, {}

    def handle_for(key):   # key = (prefix, is_direct, group); lazy open
        h = handles.get(key)
        if h is not None:
            return h
        prefix, is_direct, group = key
        path = os.path.join(
            args.out_dir,
            fname(prefix, is_direct).replace(".md", ".%s.md" % group))
        h = open(path, "w", encoding="utf-8")
        where = prefix or "entire tree"
        if is_direct:
            where += " (direct headers)"
        h.write("# Symbols: %s - %s\n\nComplete inventory, no cutouts. "
                "Grep this file; do not load it whole into an LLM context.\n\n"
                "| Symbol | Kind | Declaration | Header |\n"
                "|--------|------|-------------|--------|\n"
                % (where, GROUP_TITLE[group]))
        handles[key] = h
        counts[key] = 0
        return h

    route_cache = {}

    def route(d):
        t = route_cache.get(d)
        if t is not None:
            return t
        cur = d
        while True:
            if cur in leaf_set:
                t = (cur, False)
                break
            if cur == d and cur in direct_nodes:
                t = (cur, True)
                break
            if cur == "":
                t = ("", "" not in leaf_set)
                break
            cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
        route_cache[d] = t
        return t

    total = 0
    sql = ("SELECT d.path, s.file, s.name, s.kind, s.decl "
           "FROM symbols s JOIN dirs d ON d.id = s.dir_id" + where +
           " ORDER BY d.path, s.file")
    for dpath, file, name, kind, decl in db.execute(sql, p):
        if not keep_row(args, name, kind, decl):
            continue
        prefix, is_direct = route(dpath)
        key = (prefix, is_direct, KIND_GROUP.get(kind, "types"))
        h = handle_for(key)
        full = (dpath + "/" if dpath else "") + file
        h.write("| `%s` | %s | `%s` | `%s` |\n" %
                (name, kind, decl.replace("|", "\\|"), full))
        counts[key] += 1
        total += 1
    for h in handles.values():
        h.close()

    index_lines = ["# Symbol index by component", "",
                   "Adaptive split: no subtree exceeds %d symbols "
                   "(--max-per-file). `.types.md` files hold only "
                   "class/struct/enum/union (small - the API surface agents "
                   "read); `.defines.md` / `.vars.md` hold macros / globals "
                   "(grep them; never load them whole)." % max_per, ""]
    for key in sorted(counts,
                      key=lambda k: (k[0].count("/"), k[0], k[1], k[2])):
        prefix, is_direct, group = key
        if counts[key] == 0:
            continue
        label = prefix or "(entire tree)"
        if is_direct:
            label += " (direct headers)"
        label += " - " + group
        index_lines.append("%s- [%s](%s) - %d symbols" % (
            "  " * prefix.count("/"), label,
            fname(prefix, is_direct).replace(".md", ".%s.md" % group),
            counts[key]))
    with open(os.path.join(args.out_dir, "00-INDEX.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(index_lines) + "\n")
    print("Wrote %d files -> %s (%d symbols total, max %d per file)"
          % (len(handles), args.out_dir, total, max_per))


def cmd_symbols(args):
    with closing(open_db_required(resolve_db(args))) as db:

        if args.out_dir:
            symbols_out_dir(db, args)
            return

        sql, params = symbol_query(args)
        if getattr(args, "dedup", False):
            sql = sql.rsplit("ORDER BY", 1)[0] + \
                "ORDER BY name COLLATE NOCASE, kind, decl, path"
        cur = db.execute(sql, params)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                if getattr(args, "dedup", False):
                    n = write_symbols_dedup(f, cur, args)
                else:
                    n = write_symbols_md(f, "Symbol index", cur,
                                         "Generated %s. Complete inventory of "
                                         "exposed classes, structs, enums, "
                                         "unions, macros and global variables - "
                                         "no cutouts." % date.today().isoformat(),
                                         args)
            print("Wrote %s (%d symbols)" % (args.out, n))
        elif getattr(args, "dedup", False):
            n = 0
            cur_key, cur_paths = None, []
            for name, kind, decl, path in cur:
                if not keep_row(args, name, kind, decl):
                    continue
                key = (name, kind, decl)
                if key != cur_key:
                    if cur_key is not None:
                        print("%-40s %-7s %d header(s): %s" % (
                            cur_key[0], cur_key[1], len(cur_paths),
                            "; ".join(cur_paths)))
                        n += 1
                    cur_key, cur_paths = key, []
                cur_paths.append(path)
            if cur_key is not None:
                print("%-40s %-7s %d header(s): %s" % (
                    cur_key[0], cur_key[1], len(cur_paths), "; ".join(cur_paths)))
                n += 1
            print("%d unique symbols" % n, file=sys.stderr)
        else:
            n = 0
            for name, kind, decl, path in cur:
                if not keep_row(args, name, kind, decl):
                    continue
                print("%-40s %-7s %s" % (name, kind, path))
                n += 1
            print("%d symbols" % n, file=sys.stderr)
        if n == 0 and not (args.kind or args.pattern or
                           getattr(args, "path_prefix", None)):
            print("(0 symbols in the index - it was not built yet or predates\n"
                  " symbol support; run: python3 repoindex.py index <root>)",
                  file=sys.stderr)


def cmd_headers(args):
    root = os.path.abspath(args.root)
    patterns = args.pattern or ["*.h"]
    subdirs = args.subdir or ["."]

    matched = []
    for sub in subdirs:
        base = os.path.normpath(os.path.join(root, sub))
        if not os.path.isdir(base):
            raise ValueError("Not a directory: %s" % base)
        if args.recursive:
            walker = os.walk(base)
        else:
            walker = [(base, [], os.listdir(base))]
        for dirpath, _dirnames, filenames in walker:
            for fn in filenames:
                if any(fnmatch.fnmatch(fn, p) for p in patterns):
                    full = os.path.join(dirpath, fn)
                    if not os.path.islink(full) and os.path.isfile(full):
                        matched.append((full, fn))
    matched.sort(key=lambda t: (t[1].lower(), t[0].lower()))

    out_path = os.path.abspath(args.out) if args.out else None
    link_base = os.path.dirname(out_path) if out_path else os.getcwd()

    title = args.title or "Header index: %s" % ", ".join(subdirs)
    if args.recursive:
        scope = "all %s files under %s (recursive)" % (
            "/".join(patterns), ", ".join(subdirs))
    else:
        scope = "all direct %s files in %s" % (
            "/".join(patterns), ", ".join(subdirs))

    lines = []
    lines.append("# %s" % title)
    lines.append("")
    lines.append("Generated from %s on %s. This is an interface/include "
                 "inventory, not an exhaustive correctness audit."
                 % (scope, date.today().isoformat()))
    lines.append("")
    lines.append("%d files." % len(matched))
    lines.append("")

    unreadable = 0
    for full, fn in matched:
        rel = posix_rel(full, link_base)
        lines.append("## [%s](%s)" % (fn, rel))
        lines.append("")
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            unreadable += 1
            continue
        syms, incs = parse_header(src)
        # CAMERA_HEADERS.md format lists type declarations only; macros and
        # variables live in the symbol index (symbols table / SYMBOLS dir).
        decls = [d for (_n, k, d) in syms
                 if k in ("class", "struct", "enum", "union")]
        if decls:
            lines.append("Declarations (including forward declarations):")
            lines.append("```cpp")
            lines.extend(decls)
            lines.append("```")
        if incs:
            lines.append("Includes:")
            lines.append("```cpp")
            lines.extend('#include %s%s%s' % ("<" if s else '"', t,
                                              ">" if s else '"')
                         for t, s in incs)
            lines.append("```")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print("Wrote %s (%d files, %s)" %
              (out_path, len(matched), human_size(len(text))))
    else:
        sys.stdout.write(text)
    if unreadable:
        print("Warning: %d files unreadable" % unreadable, file=sys.stderr)


def dir_aggregates(rows):
    counts = defaultdict(int)
    sizes = defaultdict(int)
    for path, size in rows:
        parts = path.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            counts[d] += 1
            sizes[d] += size
    return counts, sizes
