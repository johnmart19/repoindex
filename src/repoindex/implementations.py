"""Complete implementation-text storage and bounded offline retrieval."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .configuration import contained

EXTENSIONS = (".cpp", ".cc")
SCHEMA = "1"

def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")

def build(db, source, extensions=EXTENSIONS):
    source = Path(source).resolve()
    extensions = tuple(sorted(set(extensions)))
    if not extensions or any(not re.fullmatch(r"\.[A-Za-z0-9+]+", ext) for ext in extensions):
        raise ValueError("Invalid implementation extensions")
    db.execute("DROP TABLE IF EXISTS implementation_fts")
    db.execute("DROP TABLE IF EXISTS implementation_files")
    db.execute("CREATE TABLE implementation_files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, size INTEGER, mtime INTEGER, source_sha256 TEXT, body_sha256 TEXT, body TEXT, lines INTEGER, normalized INTEGER)")
    placeholders = ",".join("?" for _ in extensions)
    expected = db.execute("SELECT count(*),coalesce(sum(size),0) FROM vfiles WHERE ext IN (" + placeholders + ")", extensions).fetchone()
    rows = db.execute("SELECT path,size,mtime FROM vfiles WHERE ext IN (" + placeholders + ") ORDER BY path", extensions)
    count = total = replacements = normalized_files = 0
    for rel, size, mtime in rows:
        unresolved = source / rel
        if unresolved.is_symlink() or not stat.S_ISREG(unresolved.lstat().st_mode):
            raise ValueError("Implementation is not a regular file: " + rel)
        path = contained(source, rel)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
        if (before.st_size, int(before.st_mtime)) != (size, int(mtime)) or len(raw) != size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Implementation changed since file snapshot: " + rel)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("utf-8", errors="replace")
            replacements += 1
        body = normalize(decoded)
        changed = body != decoded or "\ufffd" in decoded
        normalized_files += int(changed)
        line_count = body.count("\n") + int(bool(body) and not body.endswith("\n"))
        raw_sha = hashlib.sha256(raw).hexdigest()
        text_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        db.execute("INSERT INTO implementation_files(path,size,mtime,source_sha256,body_sha256,body,lines,normalized) VALUES(?,?,?,?,?,?,?,?)",
                   (rel, size, int(mtime), raw_sha, text_sha, body, line_count, int(changed)))
        count += 1
        total += size
        if count % 1000 == 0:
            db.commit()
        if count % 10000 == 0:
            print("  ... %d complete implementation files" % count, flush=True)
    db.commit()
    if (count, total) != expected:
        raise ValueError("Incomplete implementation read; previous bundle retained")
    # Whole-file documents preserve multiline/cross-block matches; no body sampling.
    db.execute("CREATE VIRTUAL TABLE implementation_fts USING fts5(body, content='implementation_files', content_rowid='id', tokenize='trigram', detail='none', columnsize=0)")
    print("Building implementation trigram postings for %d files ..." % count, flush=True)
    last = [time.monotonic()]
    def progress():
        if time.monotonic() - last[0] >= 20:
            print("  ... implementation postings still building", flush=True)
            last[0] = time.monotonic()
        return 0
    db.set_progress_handler(progress, 100000)
    try:
        db.execute("INSERT INTO implementation_fts(implementation_fts) VALUES('rebuild')")
        db.execute("INSERT INTO implementation_fts(implementation_fts) VALUES('optimize')")
    finally:
        db.set_progress_handler(None, 0)
    meta = {"implementation_schema": SCHEMA, "implementation_backend": "trigram",
            "implementation_extensions": json.dumps(extensions), "implementation_count": str(count),
            "implementation_bytes": str(total), "implementation_decode_errors": str(replacements),
            "implementation_normalized_files": str(normalized_files)}
    db.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta.items())
    db.commit()
    return meta

def available(db):
    value = db.execute("SELECT value FROM meta WHERE key='implementation_schema'").fetchone()
    if not value or value[0] != SCHEMA:
        raise ValueError("No implementation-content index in this snapshot; explicitly run workspace implement or workspace index once")

def scope_sql(prefixes):
    clauses, params = [], []
    for prefix in prefixes:
        if not prefix:
            clauses.append("1")
        else:
            clauses.append("(path=? OR (path>=? AND path<?))")
            params.extend([prefix, prefix + "/", prefix + "0"])
    return "(" + " OR ".join(clauses) + ")", params

def candidates_sql(pattern, prefixes):
    where, params = scope_sql(prefixes)
    if len(pattern) >= 3:
        # With detail=none each MATCH token must be <=3 codepoints. AND a bounded
        # set of overlapping trigrams, then verify the complete literal in Python.
        # False-positive candidates are allowed; exact matches cannot be omitted.
        amount = min(8, len(pattern) - 2)
        positions = {0} if amount == 1 else {i * (len(pattern) - 3) // (amount - 1) for i in range(amount)}
        tokens = sorted({pattern[i:i+3] for i in positions})
        match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        where += " AND id IN (SELECT rowid FROM implementation_fts WHERE implementation_fts MATCH ?)"
        params.append(match)
    return "SELECT id,path FROM implementation_files WHERE " + where + " ORDER BY path", params

def excerpt(body, offset, maximum):
    start = body.rfind("\n", 0, offset) + 1
    end = body.find("\n", offset)
    end = len(body) if end < 0 else end
    left = max(start, offset - min(80, maximum // 3))
    right = min(end, left + maximum)
    return ("…" if left > start else "") + body[left:right] + ("…" if right < end else ""), left > start or right < end

def search(db, pattern, prefixes, context=0, max_chars=400):
    available(db)
    pattern = normalize(pattern)
    if not pattern or len(pattern) > 4096 or not 0 <= context <= 5 or not 16 <= max_chars <= 2000:
        raise ValueError("Require 1..4096 query characters, context 0..5 and snippet size 16..2000")
    sql, params = candidates_sql(pattern, prefixes)
    for file_id, path in db.execute(sql, params):
        body = db.execute("SELECT body FROM implementation_files WHERE id=?", (file_id,)).fetchone()[0]
        offset = body.find(pattern)
        while offset >= 0:
            line = body.count("\n", 0, offset) + 1
            end_line = body.count("\n", 0, offset + len(pattern) - 1) + 1
            text, clipped = excerpt(body, offset, max_chars)
            result = {"path": path, "line": line, "end_line": end_line,
                      "column": offset - body.rfind("\n", 0, offset), "text": text}
            if clipped:
                result["truncated"] = True
            if context:
                left = body.rfind("\n", 0, offset) + 1
                first_line = line
                for _ in range(context):
                    if left == 0:
                        break
                    left = body.rfind("\n", 0, left - 1) + 1
                    first_line -= 1
                right = body.find("\n", offset + len(pattern))
                right = len(body) if right < 0 else right
                for _ in range(context):
                    following = body.find("\n", right + 1)
                    right = len(body) if following < 0 else following
                block = body[left:right]
                result.update(context_start_line=first_line, context=block[:1600])
                if len(block) > 1600:
                    result["context_truncated"] = True
            yield result
            # One record per starting line, even when the literal repeats there.
            following = body.find("\n", offset)
            offset = -1 if following < 0 else body.find(pattern, following + 1)

def validate_range(line, limit, max_chars):
    if line < 1 or not 1 <= limit <= 200 or not 16 <= max_chars <= 2000:
        raise ValueError("Require line >=1, 1..200 lines and line width 16..2000")


def read(db, path, line=1, limit=40, max_chars=400):
    validate_range(line, limit, max_chars)
    available(db)
    row = db.execute("SELECT body,source_sha256,lines FROM implementation_files WHERE path=?", (path,)).fetchone()
    if row is None:
        return None
    body, sha, total = row
    if line > total:
        return None
    lines = body.split("\n")
    chosen = lines[line-1:min(total, line-1+limit)]
    clipped = any(len(text) > max_chars for text in chosen)
    shown = [text[:max_chars] + ("…" if len(text) > max_chars else "") for text in chosen]
    return {"path": path, "start_line": line, "end_line": line + len(chosen) - 1,
            "total_lines": total, "text": "\n".join(shown), "truncated": clipped,
            "source_sha256": sha}
