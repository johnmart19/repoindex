"""Package/API boundaries; installed artifact acceptance is scripts/check_package.py."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repoindex import VERSION, database

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_database_failure_is_an_exception_not_a_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            with self.assertRaises(database.IndexReadError):
                database.open_db_required(missing)
            self.assertFalse(missing.exists())

    def test_invalid_metadata_closes_connection_without_modifying_snapshot(self):
        import sqlite3
        from unittest import mock
        connection = sqlite3.connect(":memory:")
        connection.executescript(database.SCHEMA)
        connection.executemany("INSERT INTO meta VALUES (?,?)", [
            ("schema_version", database.SCHEMA_VERSION), ("updated", "2026-10-25"),
            ("error_count", "invalid")])
        connection.commit()
        with mock.patch.object(database.sqlite3, "connect", return_value=connection):
            with self.assertRaises(database.IndexReadError):
                database.open_db_required("fixture.db")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_direct_directory_and_legacy_file_launchers(self):
        for launcher in (ROOT, ROOT / "repoindex.py"):
            result = subprocess.run([sys.executable, str(launcher), "--version"],
                                    cwd="/tmp", text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("repoindex " + VERSION, result.stdout.strip())

    def test_legacy_imports_resolve_without_installation(self):
        code = ("import repoindex, workspace_index, workspace_cli, implementation_index; "
                "from repoindex import workspace, implementations; "
                "assert workspace_index is workspace; "
                "assert implementation_index is implementations; "
                "assert callable(repoindex.parse_header); "
                "assert callable(workspace_cli.main)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_legacy_file_can_be_loaded_by_spec(self):
        spec = importlib.util.spec_from_file_location("legacy_file", ROOT / "repoindex.py")
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        self.assertEqual(VERSION, legacy.VERSION)
        self.assertEqual("Mode", legacy.parse_header("struct Mode {}; ")[0][0][0])

    def test_workspace_import_does_not_load_legacy_cli_or_reports(self):
        code = ("from repoindex import workspace; import sys; "
                "assert not any('repoindex.' + name in sys.modules "
                "for name in ('legacy_cli', 'legacy_queries', 'reports', 'guides', 'lookup'))")
        env = __import__("os").environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code], cwd="/tmp", env=env,
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
