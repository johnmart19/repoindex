"""Legacy symbol lookup and log-oriented reports."""
from contextlib import closing
import os
import re
import sys
from collections import Counter
from .database import open_db_required, resolve_db
from .scanner import IMPL_EXTS


LOG_TOKEN_RE = re.compile(r"[A-Za-z_]\w{2,}")


JUNK_TOKEN_LEN = 40


HEXISH_RE = re.compile(r"^[xX][0-9A-Fa-f_]+$")


HEXSTR_RE = re.compile(r"(?:[0-9a-f]{2}){6,}")


LOG_STOPLIST = {
    "NULL", "TRUE", "FALSE", "OK", "SUCCESS", "FAILED", "FAILURE",
    "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "VERBOSE", "TRACE",
    "FATAL", "TODO", "FIXME", "NOTE", "XXX", "YES", "NO",
    "INTERFACE", "CALLBACK", "APIENTRY", "WINAPI", "STDCALL", "EXTERN_C",
    "UNUSED", "ASSERT", "STATIC_ASSERT", "ALIGN", "PACKED", "EXPORT",
}


def plausible_symbol_token(tok):
    """CamelCase (CameraMode), Capitalized class-style words of
    length >= 5 (Timer, Session), or lower_snake_case (capture_hint).
    Plain lowercase words and short Capitalized words are skipped."""
    if any(c.isupper() for c in tok[1:]):
        return True
    if "_" in tok and not tok.isupper():
        return True
    if len(tok) >= 5 and tok[0].isupper() and tok[1:].islower():
        return True
    return False


def is_junk_token(tok):
    """Structural log junk: hex blobs, digit-heavy ids,超长 tracepoint
    names. Objective shapes, always filtered in --from-log mode."""
    if len(tok) > JUNK_TOKEN_LEN:
        return True
    if sum(ch.isdigit() for ch in tok) > len(tok) * 0.5:
        return True
    if HEXISH_RE.match(tok) or HEXSTR_RE.search(tok):
        return True
    return False


def is_forward_decl(kind, decl):
    return kind in ("class", "struct", "union") and decl.rstrip().endswith(";")


def impl_candidates(db, header_path):
    """Same-directory, same-basename implementation files (.cpp/.cc/...)."""
    dir_rel = header_path.rsplit("/", 1)[0] if "/" in header_path else ""
    base = os.path.splitext(header_path.rsplit("/", 1)[-1])[0]
    drow = db.execute("SELECT id FROM dirs WHERE path = ?",
                      (dir_rel,)).fetchone()
    if not drow:
        return []
    found = []
    for ext in IMPL_EXTS:
        if db.execute("SELECT 1 FROM files WHERE dir_id = ? AND name = ?",
                      (drow[0], base + ext)).fetchone():
            found.append((dir_rel + "/" if dir_rel else "") + base + ext)
    return found


def wrap_paths(prefix, paths, indent="    "):
    """Inline, wrapped, complete path list - for long header lists."""
    line = indent + prefix
    for p in paths:
        if len(line) + len(p) + 2 > 160:
            print(line.rstrip().rstrip(","))
            line = indent + "  " + p + ", "
        else:
            line += p + ", "
    print(line.rstrip().rstrip(","))


def lookup_symbol(db, name, substr=False):
    """-> {symbol_name: [(kind, decl, path), ...]} (empty dict = no match)."""
    if substr:
        rows = db.execute(
            "SELECT name, kind, decl, path FROM vsymbols WHERE name LIKE ? "
            "ORDER BY path", ("%" + name + "%",)).fetchall()
    else:
        rows = db.execute(
            "SELECT name, kind, decl, path FROM vsymbols WHERE name = ? "
            "ORDER BY path", (name,)).fetchall()
    by_name = {}
    for nm, kind, decl, path in rows:
        by_name.setdefault(nm, []).append((kind, decl, path))
    return by_name


def print_symbol(db, name, rows, verbose, log_count=0, max_defs=0,
                 hint_root=""):
    """One deduplicated block per symbol: each unique definition once (with
    all its headers + impl candidates), forward declarations collapsed.
    In from-log digests, symbols with many definitions collapse further."""
    kinds = sorted({k for k, _d, _p in rows})
    defs = {}
    fwd_paths = []
    for kind, decl, path in rows:
        if is_forward_decl(kind, decl):
            if path not in fwd_paths:
                fwd_paths.append(path)
        else:
            bucket = defs.setdefault((kind, decl), [])
            if path not in bucket:
                bucket.append(path)
    n_defs = sum(len(v) for v in defs.values())
    parts = []
    if n_defs:
        parts.append("%d definition%s" % (n_defs, "" if n_defs == 1 else "s"))
    if fwd_paths:
        parts.append("forward-declared in %d header%s"
                     % (len(fwd_paths), "" if len(fwd_paths) == 1 else "s"))
    if log_count:
        parts.append("seen %dx in log" % log_count)
    print("## %s (%s) - %s" % (name, "/".join(kinds),
                               "; ".join(parts) or "no data"))
    if max_defs and not verbose and n_defs > max_defs:
        kind_counts = {}
        def_headers = set()
        for (k, _d), paths in defs.items():
            kind_counts[k] = kind_counts.get(k, 0) + len(paths)
            def_headers.update(paths)
        print("  digest: %s, across %d headers"
              % (", ".join("%d %s%s" % (c, k, "" if c == 1 else "s")
                           for k, c in sorted(kind_counts.items())),
                 len(def_headers)))
        first = sorted(def_headers)[:3]
        for p in first:
            print("    @ %s" % p)
            impls = impl_candidates(db, p)
            if impls:
                print("      impl: %s" % ", ".join(impls))
        if len(def_headers) > 3:
            print("    ... and %d more headers" % (len(def_headers) - 3))
        print("  drill down: python3 repoindex.py lookup %s --root %s"
              % (name, hint_root or "<tree>"))
        print()
        return
    for (kind, decl), paths in defs.items():
        print("  def: %s" % decl)
        if len(paths) <= 10:
            for p in paths:
                print("    @ %s" % p)
                impls = impl_candidates(db, p)
                if impls:
                    print("      impl: %s" % ", ".join(impls))
        else:
            wrap_paths("defined in %d headers: " % len(paths), paths)
        if verbose:
            seen_bn = set()
            for p in paths:
                bn = p.rsplit("/", 1)[-1]
                if bn in seen_bn:
                    continue
                seen_bn.add(bn)
                inc = db.execute(
                    "SELECT DISTINCT path FROM vincludes WHERE target = ? "
                    "ORDER BY path", (bn,)).fetchall()
                if inc:
                    print("    included by (%d):" % len(inc))
                    for (ip,) in inc:
                        print("      %s" % ip)
    if fwd_paths:
        if verbose:
            wrap_paths("fwd (%d): " % len(fwd_paths), fwd_paths, indent="  ")
        else:
            print("  fwd: forward-declared in %d headers "
                  "(use --verbose to list)" % len(fwd_paths))
    print()


def print_symbol_summary(matched):
    """Ranked one-line-per-symbol table: the agent's entry point to the
    report."""
    print("=== Matched symbols, most frequent in log first ===\n")
    print("%7s  %-36s %-26s %5s  %s"
          % ("seen", "symbol", "kinds", "defs", "first definition"))
    for name, rows, lc in matched:
        kinds = "/".join(sorted({k for k, _d, _p in rows}))
        def_hdrs = {p for k, d, p in rows if not is_forward_decl(k, d)}
        first = min(def_hdrs) if def_hdrs else "(forward decls only)"
        print("%7d  %-36s %-26s %5d  %s"
              % (lc, name, kinds, len(def_hdrs), first))
    print()


def cmd_lookup(args):
    with closing(open_db_required(resolve_db(args))) as db:
        if db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0:
            print("(no symbols in index - re-run: repoindex.py index <root>)",
                  file=sys.stderr)
        root_row = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        hint_root = root_row[0] if root_row else ""

        names = list(args.names or [])
        log_counts = {}
        if args.from_log:
            try:
                with open(args.from_log, "r", encoding="utf-8",
                          errors="replace") as f:
                    text = f.read()
            except OSError as e:
                raise ValueError("Cannot read log: %s" % e)
            counts = Counter()
            stopped = junk = 0
            for tok in LOG_TOKEN_RE.findall(text):
                if not plausible_symbol_token(tok):
                    continue
                if not args.no_stoplist and tok.upper() in LOG_STOPLIST:
                    stopped += 1
                    continue
                if is_junk_token(tok):
                    junk += 1
                    continue
                counts[tok] += 1
            rare = 0
            for tok, c in counts.items():
                if c < args.min_count:
                    rare += 1
                else:
                    log_counts[tok] = c
            print("Extracted %d candidate tokens from %s"
                  % (len(log_counts), args.from_log), file=sys.stderr)
            notes = []
            if stopped:
                notes.append("%d noise" % stopped)
            if junk:
                notes.append("%d junk-shaped" % junk)
            if rare:
                notes.append("%d below --min-count %d" % (rare, args.min_count))
            if notes:
                print("Filtered out: " + ", ".join(notes) +
                      " (widen with --no-stoplist / --min-count 1)",
                      file=sys.stderr)
            # most frequent in the log first
            names.extend(sorted(log_counts, key=lambda t: (-log_counts[t], t)))

        if not names:
            raise ValueError("Give at least one symbol name, or --from-log <file>")

        explicit = bool(args.names) and not args.from_log
        verbose = args.verbose or explicit
        hits = misses = raw = 0
        matched = []   # (symbol_name, rows, log_count)
        for name in names:
            groups = lookup_symbol(db, name, substr=args.substr)
            if not groups:
                misses += 1
                if not args.from_log:
                    print("## %s - no match in index\n" % name)
                continue
            hits += 1
            for sym in sorted(groups):
                rows = groups[sym]
                raw += len(rows)
                matched.append((sym, rows, log_counts.get(name, 0)))

        if args.from_log:
            matched.sort(key=lambda t: (-t[2], t[0]))
            if not args.no_summary or args.summary_only:
                print_symbol_summary(matched)
            if not args.summary_only:
                for name, rows, lc in matched:
                    print_symbol(db, name, rows, verbose, log_count=lc,
                                 max_defs=args.max_defs, hint_root=hint_root)
        else:
            for name, rows, _lc in matched:
                print_symbol(db, name, rows, verbose)
        if args.from_log:
            print("%d/%d tokens matched a header symbol (%d unmatched; %d raw "
                  "rows collapsed into the report above)"
                  % (hits, hits + misses, misses, raw), file=sys.stderr)
