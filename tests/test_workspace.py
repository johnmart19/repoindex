import contextlib
import gzip
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repoindex import workspace as w, verification

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "original"
        self.root.mkdir()
        (self.root / "repoindex.json").write_text(json.dumps({"trees": ["alpha", "beta"], "ignore": ["out"]}))
        for name in ("alpha", "beta"):
            (self.root / name / "src").mkdir(parents=True)
            (self.root / name / "src/Mode.h").write_text("struct SharedMode {};\n")
            with contextlib.redirect_stdout(io.StringIO()):
                w.build(self.root, name, jobs=1, chunk_bytes=1024)

    def moved(self):
        moved = Path(self.temp.name) / "moved"
        shutil.copytree(self.root, moved, ignore=shutil.ignore_patterns(".cache"))
        return moved

    def test_decompression_failure_is_clean_and_does_not_publish_cache(self):
        moved = self.root / "without-cache"
        moved.mkdir()
        shutil.copy2(self.root / "repoindex.json", moved / "repoindex.json")
        shutil.copytree(self.root / "index", moved / "index")
        from repoindex import bundles
        with mock.patch.object(bundles.gzip, "open", side_effect=EOFError("truncated")):
            with self.assertRaisesRegex(ValueError, "Cannot decompress chunk"):
                w.ensure_db(moved, "alpha")
        self.assertEqual([], list((moved / ".cache/repoindex/alpha").glob("*.db")))
        self.assertEqual([], list((moved / ".cache/repoindex/alpha").glob("restore-*")))

    def test_default_both_and_optional_one(self):
        rows = list(w.query(self.root, None, "symbol", "SharedMode"))
        self.assertEqual(["alpha/src/Mode.h", "beta/src/Mode.h"], [x["path"] for x in rows])
        rows = list(w.query(self.root, ["beta"], "files", "Mode"))
        self.assertEqual(["beta/src/Mode.h"], [x["path"] for x in rows])
        rows = list(w.query(self.root, None, "files", "Mode", prefixes=["alpha/src"]))
        self.assertEqual(["alpha/src/Mode.h"], [x["path"] for x in rows])

    def test_cold_restore_after_move_never_scans(self):
        moved = self.moved()
        with mock.patch.object(w, "build_index", side_effect=AssertionError("must not scan")):
            rows = list(w.query(moved, None, "files", "Mode"))
        self.assertEqual(2, len(rows))
        for tree in ("alpha", "beta"):
            data = w.manifest(moved, tree)
            self.assertGreater(len(data["parts"]), 1)
            for part in data["parts"]:
                self.assertLessEqual(part["bytes"], w.MAX_PART)
            self.assertNotIn(str(self.root), json.dumps(data))

    def test_global_limit(self):
        self.assertEqual(1, len(list(w.query(self.root, None, "files", "Mode", limit=1))))

    def test_corrupt_chunk_refuses_restore(self):
        moved = self.moved()
        data = w.manifest(moved, "alpha")
        part = moved / "index/alpha" / data["parts"][0]["file"]
        part.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "integrity"):
            w.ensure_db(moved, "alpha")
        self.assertFalse(list((moved / ".cache/repoindex/alpha").glob("*.db")))

    def test_path_escape_and_wrong_total_are_rejected(self):
        path = self.root / "index/alpha/manifest.json"
        data = json.loads(path.read_text())
        data["parts"][0]["file"] = "../../outside.gz"
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            w.ensure_db(self.root, "alpha")
        data["parts"][0]["file"] = "part-" + data["parts"][0]["sha256"] + ".gz"
        data["database_bytes"] += 1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "sizes"):
            w.manifest(self.root, "alpha")

    def test_decompression_is_bounded(self):
        import hashlib
        moved = self.moved()
        path = moved / "index/alpha/manifest.json"
        data = json.loads(path.read_text())
        part = data["parts"][0]
        bad = gzip.compress(b"x" * (part["raw_bytes"] + 1), mtime=0)
        sha = hashlib.sha256(bad).hexdigest()
        part.update(file="part-" + sha + ".gz", sha256=sha, bytes=len(bad))
        (path.parent / part["file"]).write_bytes(bad)
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Decompressed"):
            w.ensure_db(moved, "alpha")

    def test_accelerated_files_match_legacy_for_literal_glob_and_scope(self):
        import sqlite3
        for name in ["Mode_extra.h", "name_with_underscore.h", "percent%mark.h", "bracket[xyz].h", "UpperCASE.h", "plain.h", "ab.h"]:
            (self.root / "alpha/src" / name).write_text("struct Test {};\n")
        (self.root / "alpha/src-other").mkdir()
        (self.root / "alpha/src-other/Mode.h").write_text("struct Sibling {};\n")
        w.build(self.root, "alpha", jobs=1)
        db = sqlite3.connect(w.ensure_db(self.root, "alpha"))
        self.addCleanup(db.close)
        patterns = ["Mode", "mode", "*.h", "Mode*.h", "?ode.h", "*[a-z]ode*", "name_with_underscore", "percent%mark", "bracket[xyz]", "UpperCASE", "ab", "no_match_43836", "src/Mode*.h"]
        for prefix in ("", "src"):
            for pattern in patterns:
                sql, params = w.files_sql(db, pattern, [prefix], 100)
                accelerated = db.execute(sql, params).fetchall()
                # Original public matching contract, independently evaluated on the source view.
                cond, args = ("(path GLOB ? OR name GLOB ?)", [pattern, pattern]) if any(c in pattern for c in "*?") else ("instr(lower(path),lower(?))>0", [pattern])
                if prefix:
                    cond += " AND (path=? OR substr(path,1,?)=?)"
                    args += [prefix, len(prefix) + 1, prefix + "/"]
                legacy = db.execute("SELECT path FROM vfiles WHERE " + cond + " ORDER BY path LIMIT 100", args).fetchall()
                self.assertEqual(legacy, accelerated, (pattern, prefix))
        rows = list(w.query(self.root, ["alpha"], "files", "Mode", prefixes=["alpha/src/"]))
        self.assertTrue(rows)
        self.assertTrue(all("src-other" not in row["path"] for row in rows))

    def test_paths_backend_when_trigram_extension_is_unavailable(self):
        import sqlite3
        with contextlib.closing(sqlite3.connect(w.ensure_db(self.root, "alpha"))) as db, db:
            db.execute("DELETE FROM meta WHERE key IN ('search_schema','search_backend')")
            class WithoutTrigram:
                def execute(self, sql, *args):
                    if sql.startswith("CREATE VIRTUAL TABLE"):
                        raise sqlite3.OperationalError("no such tokenizer: trigram")
                    return db.execute(sql, *args)
                def executemany(self, *args):
                    return db.executemany(*args)
            w.prepare_search(WithoutTrigram())
            sql, params = w.files_sql(db, "Mode", [""], 20)
            self.assertEqual([("src/Mode.h",)], db.execute(sql, params).fetchall())
            self.assertEqual("paths", db.execute("SELECT value FROM meta WHERE key='search_backend'").fetchone()[0])

    def test_warm_queries_do_not_wait_for_rebuild_lock(self):
        import subprocess
        w.ensure_db(self.root, "alpha")
        with w.lock(self.root, "alpha"):
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]), "workspace", "search", "Mode", "--workspace", str(self.root), "--tree", "alpha"], capture_output=True, text=True, timeout=3)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("alpha/src/Mode.h", result.stdout)

    def test_verify_live_detects_changed_and_extra_source(self):
        incomplete = w.manifest(self.root, "alpha")
        incomplete["metadata"]["error_count"] = "1"
        with mock.patch.object(verification, "manifest", return_value=incomplete):
            with self.assertRaisesRegex(ValueError, "incomplete scan"):
                w.verify(self.root, "alpha")
        self.assertTrue(w.verify(self.root, "alpha", live=True)["live_metadata_verified"])
        (self.root / "alpha/src/Mode.h").write_text("struct ChangedAndLonger {};\n")
        with self.assertRaisesRegex(ValueError, "Live metadata mismatch"):
            w.verify(self.root, "alpha", live=True)
        (self.root / "alpha/new.h").write_text("extra")
        with self.assertRaisesRegex(ValueError, "extra"):
            w.verify(self.root, "alpha", live=True)

    def test_trigram_and_ordered_path_query_plans(self):
        import sqlite3
        with contextlib.closing(sqlite3.connect(w.ensure_db(self.root, "alpha"))) as db, db:
            sql, params = w.files_sql(db, "SharedMode", [""], 20)
            plan = str(db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            if w.manifest(self.root, "alpha")["metadata"]["search_backend"] == "trigram":
                self.assertIn("VIRTUAL TABLE", plan)
            sql, params = w.files_sql(db, "*.h", [""], 20)
            plan = str(db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            self.assertNotIn("TEMP B-TREE", plan)

    def test_failed_build_preserves_published_manifest(self):
        path = self.root / "index/alpha/manifest.json"
        old = path.read_bytes()
        with mock.patch.object(w, "build_index", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                w.build(self.root, "alpha")
        self.assertEqual(old, path.read_bytes())
        self.assertEqual(1, len(list(w.query(self.root, ["alpha"], "files", "Mode"))))

if __name__ == "__main__":
    unittest.main()
