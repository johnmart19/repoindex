import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("repoindex", Path(__file__).resolve().parents[1] / "repoindex.py")
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class RepoindexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "test.h").write_text("struct CameraMode { int value; };\n#include <stdint.h>\n")
        self.db = self.root / ".repoindex.db"

    def index(self, jobs=1, **kw):
        opts = dict(root=str(self.root), db=str(self.db), no_ignore=False,
                    ignore=[], no_symbols=False, jobs=jobs)
        opts.update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            r.cmd_index(argparse.Namespace(**opts))

    def guide(self, **kw):
        opts = dict(root=str(self.root), db=str(self.db), reindex=False,
                    ignore=[], compact=True, title=None, out=str(self.root / "AGENTS.md"),
                    also=None, replace=False, depth=2, key_files=False)
        opts.update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            r.cmd_agentsmd(argparse.Namespace(**opts))

    def test_missing_query_does_not_create_database(self):
        with self.assertRaises(SystemExit):
            r.open_db_required(str(self.db))
        self.assertFalse(self.db.exists())

    def test_invalid_database_is_not_modified(self):
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE unrelated(value)")
        before = self.db.read_bytes()
        with self.assertRaises(SystemExit):
            r.open_db_required(str(self.db))
        self.assertEqual(before, self.db.read_bytes())

    def test_query_connection_is_read_only(self):
        self.index()
        db = r.open_db_required(str(self.db))
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM files")

    def test_guide_requires_explicit_scan(self):
        with mock.patch.object(r, "cmd_index") as scan:
            with self.assertRaises(SystemExit):
                self.guide()
            scan.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_guide_refuses_wrong_root(self):
        self.index()
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(SystemExit):
            self.guide(root=str(other))
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_compact_avoids_full_inventory_aggregation(self):
        self.index()
        with mock.patch.object(r, "dir_aggregates", side_effect=AssertionError("full scan")):
            self.guide()
        self.assertIn("snapshot", (self.root / "AGENTS.md").read_text())

    def test_curated_text_survives_regeneration(self):
        self.index()
        self.guide()
        dest = self.root / "AGENTS.md"
        dest.write_text("My project rules\n" + dest.read_text() + "\nManual notes\n")
        self.guide(title="New title")
        result = dest.read_text()
        self.assertTrue(result.startswith("My project rules\n"))
        self.assertTrue(result.endswith("\nManual notes\n"))
        self.assertEqual(1, result.count(r.GUIDE_BEGIN))
        self.assertIn("New title", result)

    def test_unmarked_file_is_protected(self):
        self.index()
        dest = self.root / "AGENTS.md"
        dest.write_text("Do not rebuild ROMs\n")
        with self.assertRaises(SystemExit):
            self.guide()
        self.assertEqual("Do not rebuild ROMs\n", dest.read_text())
        self.guide(replace=True)
        self.assertIn(r.GUIDE_BEGIN, dest.read_text())

    def test_alias_conflict_preflight_preserves_primary(self):
        self.index()
        self.guide()
        original = (self.root / "AGENTS.md").read_text()
        (self.root / "CLAUDE.md").write_text("Handwritten")
        with self.assertRaises(SystemExit):
            self.guide(also="CLAUDE.md", title="Changed")
        self.assertEqual(original, (self.root / "AGENTS.md").read_text())

    def test_symlink_output_is_protected(self):
        self.index()
        original = self.root / "rules.md"
        original.write_text("Manual")
        (self.root / "AGENTS.md").symlink_to(original)
        with self.assertRaises(SystemExit):
            self.guide(replace=True)
        self.assertEqual("Manual", original.read_text())

    def test_file_symlinks_and_special_headers_are_not_parsed(self):
        (self.root / "alias.h").symlink_to(self.root / "test.h")
        os.mkfifo(self.root / "pipe.h")
        self.index()
        db = r.open_db_required(str(self.db))
        self.addCleanup(db.close)
        paths = {x[0] for x in db.execute("SELECT path FROM vsymbols")}
        self.assertEqual({"test.h"}, paths)
        self.assertEqual(3, db.execute("SELECT count(*) FROM files").fetchone()[0])

    def test_similarly_named_db_file_is_not_excluded(self):
        (self.root / ".repoindex.db.notes").write_text("not a database journal")
        self.index()
        db = r.open_db_required(str(self.db))
        self.addCleanup(db.close)
        self.assertIn((".repoindex.db.notes",), db.execute("SELECT path FROM vfiles").fetchall())

    def test_status_records_scope_without_live_scan(self):
        self.index(no_symbols=True, ignore=["out"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            r.cmd_status(argparse.Namespace(db=str(self.db), root=str(self.root), json=True))
        data = json.loads(output.getvalue())
        self.assertFalse(data["live_tree_verified"])
        self.assertTrue(data["coverage_known"])
        self.assertEqual("false", data["metadata"]["symbols_enabled"])
        self.assertIn("out", json.loads(data["metadata"]["ignore_directories"]))

    def test_parallel_and_serial_have_same_records(self):
        self.index()
        def read():
            with sqlite3.connect(self.db) as db:
                return [db.execute("SELECT * FROM " + v + " ORDER BY 1,2").fetchall()
                        for v in ("vfiles", "vsymbols", "vincludes")]
        first = read()
        self.index(jobs=2)
        self.assertEqual(first, read())

    def test_full_guide_also_preserves_curated_text(self):
        self.index()
        self.guide(compact=False)
        self.assertIn("Directory map", (self.root / "AGENTS.md").read_text())

    def test_cli_defaults_to_compact_and_full_is_explicit(self):
        self.index()
        tool = str(Path(r.__file__))
        compact = subprocess.check_output([sys.executable, tool, "agentsmd", str(self.root)], text=True)
        self.assertNotIn("## Directory map", compact)
        full = subprocess.check_output([sys.executable, tool, "agentsmd", str(self.root), "--full"], text=True)
        self.assertIn("## Directory map", full)

    def test_old_schema2_coverage_remains_unknown(self):
        self.index()
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM meta WHERE key IN ('error_count','ignore_directories','symbols_enabled')")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            r.cmd_status(argparse.Namespace(db=str(self.db), root=str(self.root), json=True))
        self.assertFalse(json.loads(output.getvalue())["coverage_known"])

    def test_scan_errors_are_retained_and_warn_readers(self):
        original = r.scan_directory
        def fail_one(*args):
            files, symbols, includes, errors = original(*args)
            return files, symbols, includes, errors + 1
        with mock.patch.object(r, "scan_directory", side_effect=fail_one):
            self.index()
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            db = r.open_db_required(str(self.db))
        self.addCleanup(db.close)
        self.assertIn("incomplete", warning.getvalue())
        self.assertEqual("1", db.execute("SELECT value FROM meta WHERE key='error_count'").fetchone()[0])

    def test_guide_uses_snapshot_date_not_generation_date(self):
        self.index()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE meta SET value='2000-01-01' WHERE key='updated'")
        self.guide(compact=False)
        self.assertIn("Index snapshot: 2000-01-01", (self.root / "AGENTS.md").read_text())

    def test_malformed_markers_are_rejected(self):
        for content in [r.GUIDE_END + r.GUIDE_BEGIN,
                        r.GUIDE_BEGIN * 2 + r.GUIDE_END,
                        r.GUIDE_BEGIN]:
            with self.assertRaises(ValueError):
                r.merge_guide(content, "new")


if __name__ == "__main__":
    unittest.main()
