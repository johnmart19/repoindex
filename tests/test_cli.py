"""Standalone CLI contracts, migrated from the consumer wrapper suite."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repoindex import workspace_cli as cli
from repoindex import workspace as workspace


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = ["alpha", "beta"]
        config = {"trees": self.names, "ignore": [], "guide": "docs/INDEX_GUIDE.md",
                  "scopes": {"core": ["beta/core"], "drivers": ["alpha/drivers"]},
                  "benchmarks": [{"mode": "impl", "query": "run_body", "scope": "core"}]}
        (self.root / "repoindex.json").write_text(json.dumps(config))
        (self.root / ".rgignore").write_text(".cache/**\n")
        (self.root / "docs").mkdir()
        for folder in ("beta/core", "beta/core-extra", "alpha/drivers"):
            path = self.root / folder
            path.mkdir(parents=True)
            (path / "Mode.h").write_text("struct UniqueMode {};\nneedle\nneedle\n")
            (path / "Impl.cpp").write_text("int dispatch() {\n  return run_body();\n}\n")
        for tree in self.names:
            workspace.build(self.root, tree, jobs=1)

    def query(self, args, json_output=True):
        output, errors = io.StringIO(), io.StringIO()
        flags = ["--workspace", str(self.root)] + (["--json"] if json_output else [])
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(args + flags)
        self.assertEqual(0, code, errors.getvalue())
        return output.getvalue().splitlines()

    def test_file_and_symbol_scope_exclude_sibling(self):
        for mode in ("files", "symbol"):
            rows = [json.loads(x) for x in self.query([mode, "UniqueMode" if mode == "symbol" else "Mode", "--scope", "core"])]
            self.assertEqual(["beta/core/Mode.h"], [x["path"] for x in rows])

    def test_default_both_and_optional_tree(self):
        self.assertEqual(3, len(self.query(["files", "Mode"])))
        self.assertEqual(1, len(self.query(["files", "Mode", "--tree", "alpha"])))
        self.assertEqual([], self.query(["files", "Mode", "--scope", "core", "--tree", "alpha"]))

    def test_text_stops_at_global_limit(self):
        rows = self.query(["text", "needle", "--limit", "1"], json_output=False)
        self.assertEqual(1, len(rows))
        self.assertIn("Mode.h:2:needle", rows[0])

    def test_indexed_implementation_default_both_and_scope(self):
        self.assertEqual(3, len(self.query(["impl", "run_body"])))
        rows = [json.loads(line) for line in self.query(["impl", "run_body", "--scope", "core", "--context", "1"])]
        self.assertEqual(1, len(rows))
        self.assertEqual("beta/core/Impl.cpp", rows[0]["path"])
        self.assertEqual(2, rows[0]["line"])

    def test_indexed_source_line_range(self):
        rows = [json.loads(line) for line in self.query(["source", "beta/core/Impl.cpp", "--line", "2", "--limit", "1"])]
        self.assertEqual("  return run_body();", rows[0]["text"])
        self.assertEqual([], self.query(["source", "beta/core/Impl.cpp", "--scope", "drivers"]))

    def test_selected_refresh_generates_portable_guide(self):
        before = (self.root / "index/alpha/manifest.json").read_bytes()
        self.query(["index", "--tree", "beta", "--jobs", "1"])
        self.assertEqual(before, (self.root / "index/alpha/manifest.json").read_bytes())
        text = (self.root / "docs/INDEX_GUIDE.md").read_text()
        self.assertNotIn(str(self.root), text)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)

    def test_compact_human_output_is_default(self):
        rows = self.query(["impl", "run_body", "--scope", "core"], json_output=False)
        self.assertEqual(["beta/core/Impl.cpp:2:   return run_body();"], rows)
        self.assertNotIn("declaration", rows[0])

    def test_configured_benchmark_has_no_consumer_dependency(self):
        path = self.root / "measurements.json"
        rows = [json.loads(line) for line in self.query(["benchmark", "--samples", "3", "--out", str(path)])]
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["rows"])
        self.assertEqual(3, len(json.loads(path.read_text())[0]["samples_ms"]))
        compared = [json.loads(line) for line in self.query(["benchmark", "--samples", "3", "--compare", str(path)])]
        self.assertEqual(rows[0]["median_ms"], compared[0]["before_ms"])
        historical = json.loads(path.read_text())
        historical[0]["pattern"] = historical[0].pop("query")
        path.write_text(json.dumps(historical))
        compared = [json.loads(line) for line in self.query(["benchmark", "--samples", "3", "--compare", str(path)])]
        self.assertIn("speedup", compared[0])

    def test_find_workspace_from_subdirectory(self):
        child = self.root / "arbitrary/nested"
        child.mkdir(parents=True)
        self.assertEqual(self.root, cli.find_workspace(child))

    def test_guide_preserves_curated_prose_and_refuses_unmarked_file(self):
        path = self.root / "docs/INDEX_GUIDE.md"
        path.write_text("Handwritten instructions\n")
        with self.assertRaises(ValueError):
            cli.write_guide(self.root)
        self.assertEqual("Handwritten instructions\n", path.read_text())
        cli.write_guide(self.root, replace=True)
        path.write_text("Manual prefix\n" + path.read_text() + "Manual suffix\n")
        cli.write_guide(self.root)
        self.assertTrue(path.read_text().startswith("Manual prefix\n"))
        self.assertTrue(path.read_text().endswith("Manual suffix\n"))

    def test_source_free_workspace_queries_without_building(self):
        offline = self.root / "offline"
        offline.mkdir()
        shutil.copy2(self.root / "repoindex.json", offline / "repoindex.json")
        shutil.copytree(self.root / "index", offline / "index")
        output = io.StringIO()
        with mock.patch.object(workspace, "build", side_effect=AssertionError("must not build")), contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(["impl", "run_body", "--workspace", str(offline), "--json"]))
        self.assertEqual(3, len(output.getvalue().splitlines()))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, cli.main(["text", "needle", "--workspace", str(offline)]))

    def test_output_limit_never_cancels_explicit_maintenance(self):
        called = []
        def fake_build(root, tree, *args):
            called.append(tree)
            return {"tree": tree, "large_result": "x" * 1000}
        with mock.patch.object(workspace, "build", side_effect=fake_build), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["index", "--workspace", str(self.root), "--max-output-bytes", "128", "--json"])
        self.assertEqual(self.names, called)

    def test_bad_scope_and_ambiguous_index_argument_never_build(self):
        with mock.patch.object(workspace, "build") as build, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, cli.main(["files", "Mode", "--scope", "missing", "--workspace", str(self.root)]))
            self.assertEqual(2, cli.main(["index", "unexpected", "--workspace", str(self.root)]))
        build.assert_not_called()


class OutputTests(unittest.TestCase):
    def test_budget_stops_producer_and_keeps_json_records_valid(self):
        consumed = []
        def stream():
            for index in range(1000000):
                consumed.append(index)
                yield {"path": "alpha/file-%d.cpp" % index}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cli.emit(stream(), json_output=True, budget=128))
        self.assertLessEqual(len(output.getvalue().encode()), 128)
        self.assertLess(len(consumed), 6)
        for line in output.getvalue().splitlines():
            self.assertIn("path", json.loads(line))

    def test_huge_json_record_is_not_partially_emitted(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli.emit([{"path": "alpha/a.cpp", "text": "x" * 10000}], json_output=True, budget=128)
        self.assertEqual(3, code)
        self.assertEqual("", output.getvalue())

    def test_human_budget_is_utf8_safe(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            cli.emit([{"path": "é" * 200}], budget=129)
        self.assertLessEqual(len(output.getvalue().encode()), 129)
        self.assertNotIn("�", output.getvalue())

    def test_symbol_display_is_bounded_without_altering_input(self):
        original = {"path": "a/file.h", "kind": "define", "declaration": "x" * 10000}
        text = cli.format_record(original, width=64)
        self.assertLess(len(text), 100)
        self.assertEqual(10000, len(original["declaration"]))

    def test_help_does_not_load_a_workspace(self):
        with mock.patch.object(workspace, "config") as load, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.main(["--help"])
        self.assertEqual(0, result.exception.code)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
