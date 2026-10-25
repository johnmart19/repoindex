"""Malformed input and failed publication never broaden selection or leave staging files."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repoindex import bundles, configuration, workspace_cli, workspace
from unittest import mock


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_bad_configuration_fields_fail_at_boundary(self):
        for value in [[], {"trees": ["alpha"], "ignore": "out"},
                      {"trees": ["alpha"], "ignore": ["a/b"]},
                      {"trees": ["alpha"], "implementation_extensions": ".cpp"}]:
            (self.root / "repoindex.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                configuration.config(self.root)

    def test_paths_are_normalized_and_overlapping_inputs_collapsed(self):
        data = {"trees": ["alpha", "beta"]}
        self.assertEqual(["alpha/src"], configuration.selected_paths(self.root, data,
            data["trees"], prefixes=["alpha/./src/child", "alpha/src/", "alpha/src"]))
        self.assertEqual([], configuration.selected_paths(self.root, data, ["beta"], prefixes=["alpha/src"]))
        for prefix in ["unknown/src", "alpha/../beta"]:
            with self.assertRaises(ValueError):
                configuration.selected_paths(self.root, data, data["trees"], prefixes=[prefix])

    def test_failed_json_serialization_preserves_target_and_removes_temporary(self):
        target = self.root / "manifest.json"
        target.write_text("old")
        with self.assertRaises(TypeError):
            bundles.atomic_json(target, {"invalid": object()})
        self.assertEqual("old", target.read_text())
        self.assertEqual([target], list(self.root.iterdir()))

    def test_nonobject_manifest_is_rejected_without_traceback(self):
        directory = self.root / "index/alpha"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("[]")
        with self.assertRaisesRegex(ValueError, "identity/format"):
            bundles.manifest(self.root, "alpha")

    def test_failed_atomic_replace_preserves_target_and_cleans_staging(self):
        target = self.root / "guide.md"
        target.write_text("old")
        with mock.patch.object(bundles.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                bundles.atomic_text(target, "new")
        self.assertEqual("old", target.read_text())
        self.assertEqual([target], list(self.root.iterdir()))

    def test_invalid_benchmarks_fail_before_any_cache_restoration(self):
        data = {"trees": ["alpha"], "benchmarks": [{}]}
        with mock.patch.object(workspace_cli.workspace, "ensure_db") as restore:
            with self.assertRaises(ValueError):
                workspace_cli.benchmark(self.root, ["alpha"], data)
            data["benchmarks"] = [{"mode": "files", "query": "file"}]
            with self.assertRaises(ValueError):
                workspace_cli.benchmark(self.root, ["alpha"], data, budget=float("nan"))
        restore.assert_not_called()

    def test_invalid_source_range_does_not_restore_a_large_database(self):
        with mock.patch.object(workspace, "ensure_db") as restore:
            for line, limit in [(0, 1), (1, 201)]:
                with self.assertRaises(ValueError):
                    list(workspace.read_source(self.root, None, "alpha/file.cpp", line=line, limit=limit))
        restore.assert_not_called()
