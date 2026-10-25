import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import implementation_index as impl
import workspace_index as w

class ImplementationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "original"
        self.root.mkdir()
        (self.root / "repoindex.json").write_text(json.dumps({"trees": ["alpha", "beta"], "ignore": []}))
        self.body = "namespace sample {\nint Engine::run(int value) {\n  const char* key = \"Needle_TOKEN\";\n  if (value > 0) {\n    return nested(value);\n  }\n  return -1;\n}\n// quote \" [] % _ café_lambda\n// needle needle\n}\n"
        for tree in ("alpha", "beta"):
            folder = self.root / tree / "src"
            folder.mkdir(parents=True)
            (folder / "Mode.h").write_text("struct Mode {};\n")
            (folder / "engine.cpp").write_text(self.body)
            (folder / "helper.cc").write_text("int helper() { return helper_body(42); }\n")
            w.build(self.root, tree, jobs=1, chunk_bytes=1024)

    def test_all_files_bodies_and_raw_hashes(self):
        meta = w.manifest(self.root, "alpha")["metadata"]
        self.assertEqual("2", meta["implementation_count"])
        self.assertEqual(str(len(self.body.encode()) + (self.root / "alpha/src/helper.cc").stat().st_size), meta["implementation_bytes"])
        with sqlite3.connect(w.ensure_db(self.root, "alpha")) as db:
            body, sha = db.execute("SELECT body,source_sha256 FROM implementation_files WHERE path='src/engine.cpp'").fetchone()
        self.assertEqual(self.body, body)
        self.assertEqual(hashlib.sha256(self.body.encode()).hexdigest(), sha)

    def test_literal_results_match_live_text_with_special_characters(self):
        patterns = ["Needle_TOKEN", "Engine::run", "return nested", 'quote " [] % _', "café_lambda", "needle", "return", "if", "_", "absent_97236", "needle_TOKEN"]
        patterns += [self.body[start:start+width] for start in range(0, len(self.body)-12, 7) for width in (3, 4, 8, 12)]
        for pattern in patterns:
            rows = list(w.query(self.root, ["alpha"], "impl", pattern, limit=100))
            expected = []
            for file in sorted((self.root / "alpha/src").glob("*")):
                if file.suffix not in impl.EXTENSIONS:
                    continue
                body = impl.normalize(file.read_text())
                line_numbers = sorted({body.count("\n", 0, match.start()) + 1 for match in re.finditer("(?=" + re.escape(pattern) + ")", body)})
                expected += [("alpha/src/" + file.name, number) for number in line_numbers]
            self.assertEqual(expected, [(row["path"], row["line"]) for row in rows], pattern)

    def test_multiline_body_match_across_many_lines(self):
        path = self.root / "alpha/src/engine.cpp"
        path.write_text("\n".join(["// preceding"] * 63 + ["last_before_block", "first_after_block", "return final_body();"]) + "\n")
        w.build(self.root, "alpha", jobs=1)
        rows = list(w.query(self.root, ["alpha"], "impl", "last_before_block\nfirst_after_block"))
        self.assertEqual(1, len(rows))
        self.assertEqual((64, 65), (rows[0]["line"], rows[0]["end_line"]))
        self.assertTrue(list(w.query(self.root, ["alpha"], "impl", "final_body")))

    def test_default_both_scopes_and_global_limit(self):
        rows = list(w.query(self.root, None, "impl", "Needle_TOKEN"))
        self.assertEqual(["alpha", "beta"], [row["tree"] for row in rows])
        rows = list(w.query(self.root, None, "impl", "Needle_TOKEN", prefixes=["beta/src/"]))
        self.assertEqual(["beta"], [row["tree"] for row in rows])
        self.assertEqual(1, len(list(w.query(self.root, None, "impl", "return", limit=1))))

    def test_cold_search_and_source_without_source_folders(self):
        moved = Path(self.temp.name) / "moved"
        moved.mkdir()
        shutil.copy2(self.root / "repoindex.json", moved / "repoindex.json")
        shutil.copytree(self.root / "index", moved / "index")
        self.assertFalse((moved / "alpha").exists())
        rows = list(w.query(moved, None, "impl", "nested(value)"))
        self.assertEqual(2, len(rows))
        blocks = list(w.read_source(moved, None, "alpha/src/engine.cpp", line=2, limit=4))
        self.assertEqual(1, len(blocks))
        self.assertEqual((2, 5), (blocks[0]["start_line"], blocks[0]["end_line"]))
        self.assertIn("return nested(value)", blocks[0]["text"])
        self.assertEqual(2, w.verify(moved, "alpha")["implementation_files"])

    def test_display_limits_do_not_truncate_stored_bodies(self):
        path = self.root / "alpha/src/engine.cpp"
        body = "x" * 4000 + "rare_body_token" + "z" * 4000 + "\nnext_line\n"
        path.write_text(body)
        w.build(self.root, "alpha", jobs=1)
        rows = list(w.query(self.root, ["alpha"], "impl", "rare_body_token", context=1, max_chars=100))
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["truncated"])
        self.assertIn("rare_body_token", rows[0]["text"])
        self.assertLessEqual(len(rows[0]["text"]), 102)
        self.assertLessEqual(len(rows[0]["context"]), 1600)
        with sqlite3.connect(w.ensure_db(self.root, "alpha")) as db:
            self.assertEqual(body, db.execute("SELECT body FROM implementation_files WHERE path='src/engine.cpp'").fetchone()[0])

    def test_decode_and_newline_normalization_preserves_searchable_tail(self):
        path = self.root / "alpha/src/engine.cpp"
        raw = b'int crlf() {\r\nreturn 1; }\r\n' + bytes([0, 255]) + b' after_nul();\r\n'
        path.write_bytes(raw)
        w.build(self.root, "alpha", jobs=1)
        rows = list(w.query(self.root, ["alpha"], "impl", "after_nul"))
        self.assertEqual(1, len(rows))
        self.assertEqual(3, rows[0]["line"])
        block = list(w.read_source(self.root, ["alpha"], "alpha/src/engine.cpp"))[0]
        self.assertEqual(hashlib.sha256(raw).hexdigest(), block["source_sha256"])
        self.assertEqual("1", w.manifest(self.root, "alpha")["metadata"]["implementation_decode_errors"])

    def test_live_hash_verification_detects_same_size_same_time_edit(self):
        self.assertTrue(w.verify(self.root, "alpha", live=True, hash_implementations=True)["implementation_hashes_verified"])
        path = self.root / "alpha/src/engine.cpp"
        info = path.stat()
        path.write_bytes(path.read_bytes().replace(b'Needle_TOKEN', b'Other__TOKEN'))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertTrue(w.verify(self.root, "alpha", live=True)["live_metadata_verified"])
        with self.assertRaisesRegex(ValueError, "implementation SHA-256"):
            w.verify(self.root, "alpha", live=True, hash_implementations=True)

    def test_changed_source_upgrade_keeps_previous_bundle(self):
        path = self.root / "index/alpha/manifest.json"
        before = path.read_bytes()
        (self.root / "alpha/src/engine.cpp").write_text("different size\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            w.optimize(self.root, "alpha", implementations=True)
        self.assertEqual(before, path.read_bytes())
        self.assertTrue(list(w.query(self.root, ["alpha"], "impl", "Needle_TOKEN")))

if __name__ == "__main__":
    unittest.main()
