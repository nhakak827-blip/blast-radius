"""Synthetic regression checks for patch interpretation and selection."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from selector import Impact, apply_hunks, optimized_workers, parse_patch, probe


class PatchTests(unittest.TestCase):
    def test_multiple_hunks_and_files(self):
        patch = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1,2 @@\n-first\n+FIRST\n+extra\n"
            "@@ -3 +4 @@\n-third\n+THIRD\n"
            "--- a/tests/data.sql\n+++ b/tests/data.sql\n"
            "@@ -1 +1 @@\n-SELECT 1\n+SELECT 2\n"
        )
        code, fixture = parse_patch(patch.replace("\n", "\r\n"))
        self.assertEqual(apply_hunks("first\nsecond\nthird\n", code.hunks),
                         "FIRST\nextra\nsecond\nTHIRD\n")
        self.assertEqual(fixture.after, "tests/data.sql")

    def test_addition_and_deletion_ranges(self):
        append, remove = parse_patch(
            "--- a/a.py\n+++ b/a.py\n@@ -1,0 +2 @@\n+added\n"
            "--- a/b.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n"
        )
        self.assertEqual(apply_hunks("original\n", append.hunks), "original\nadded\n")
        self.assertEqual(remove.after, "")

    def test_malformed_patch_or_wrong_checkout(self):
        for patch in ("", "--- a/a\n+++ b/a\n@@ -1,2 +1 @@\n-x\n+y\n",
                      "--- a/../a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n"):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                parse_patch(patch)
        edit, = parse_patch("--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n")
        with self.assertRaises(ValueError):
            apply_hunks("wrong\n", edit.hunks)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sqlglot").mkdir()
        self.source = "def run(flag):\n    if flag:\n        return 1\n    return 2\n"
        (self.root / "sqlglot/core.py").write_text(self.source, encoding="utf-8")
        self.patch = self.root / "x.patch"
        index = {
            "ids": ["first", "second"], "masks": ["0", "1", "2", "3"],
            "files": {"sqlglot/core.py": {"2": 3, "3": 1, "4": 2}},
            "fixtures": {"tests/data.sql": 2},
        }
        tests = [{"number": n, "id": test_id} for n, test_id in enumerate(
            ["first", "second", "unindexed"])]
        self.impact = Impact(index, self.root, tests)

    def selected(self, hunk, path="sqlglot/core.py"):
        self.patch.write_text(f"--- a/{path}\n+++ b/{path}\n{hunk}", encoding="utf-8")
        tests, _, _ = self.impact.select(self.patch)
        return [test["number"] for test in tests]

    def test_line_coverage_and_unindexed_tests(self):
        self.assertEqual(self.selected("@@ -3 +3 @@\n-        return 1\n+        return 3\n"), [0, 2])

    def test_inserted_branch(self):
        self.assertEqual(self.selected("@@ -3,0 +4 @@\n+    print(flag)\n"), [0, 1, 2])

    def test_fixture_reader(self):
        self.assertEqual(self.selected("@@ -1 +1 @@\n-a\n+b\n", "tests/data.sql"), [1, 2])

    def test_syntax_error_keeps_all_tests(self):
        self.assertEqual(self.selected("@@ -3 +3 @@\n-        return 1\n+return 42\n"), [0, 1, 2])

    def test_ast_equivalent_comment(self):
        self.assertEqual(self.selected("@@ -1,0 +2 @@\n+    # comment\n"), [2])

    def test_import_change_keeps_all_tests(self):
        self.assertEqual(self.selected("@@ -0,0 +1 @@\n+import missing_package\n"), [0, 1, 2])

    def test_mixed_input_commit_keeps_candidates_out_of_probe(self):
        self.impact.fixtures["tests/data.sql"] = 1
        self.patch.write_text(
            "--- a/sqlglot/core.py\n+++ b/sqlglot/core.py\n"
            "@@ -3 +3 @@\n-        return 1\n+        return 3\n"
            "--- a/tests/data.sql\n+++ b/tests/data.sql\n"
            "@@ -1 +1 @@\n-a\n+b\n",
            encoding="utf-8",
        )
        selected, sources, probeable = self.impact.select(self.patch)
        self.assertEqual([test["number"] for test in selected], [0, 2])
        self.assertIn("sqlglot/core.py", sources)
        self.assertEqual(probeable, [])

    def test_custom_equality_does_not_imply_identity(self):
        (self.root / "sqlglot/table.py").write_text(
            "class Table:\n    def __eq__(self, other):\n        return other is None\n",
            encoding="utf-8",
        )
        source = (
            "from sqlglot.table import Table\n"
            "class Context:\n"
            "    def run(self):\n"
            "        self.value: Table | None = Table()\n"
            "        return self.value is None\n"
        )
        (self.root / "sqlglot/core.py").write_text(source, encoding="utf-8")
        self.impact.files["sqlglot/core.py"]["5"] = 1
        self.assertEqual(self.selected("@@ -5 +5 @@\n"
                                       "-        return self.value is None\n"
                                       "+        return self.value == None\n"), [0, 2])


class SchedulerTests(unittest.TestCase):
    def test_worker_count_adapts(self):
        self.assertEqual(optimized_workers([], {}), [])
        self.assertEqual(optimized_workers([1, 2], {1: 500, 2: 900}), [[2, 1]])
        durations = dict.fromkeys(range(12), 20_000)
        workers = optimized_workers(durations, durations)
        self.assertEqual(len(workers), 6)
        self.assertCountEqual([test for worker in workers for test in worker], durations.keys())


@unittest.skipUnless(importlib.util.find_spec("pytest_timeout"), "locked pytest tools required")
class ProbeTests(unittest.TestCase):
    def test_subtest_failure_and_ordinary_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text("value = 1\n", encoding="utf-8")
            (root / "test_example.py").write_text(
                "import unittest\nfrom example import value\n"
                "class Cases(unittest.TestCase):\n"
                "    def test_subtest(self):\n"
                "        with self.subTest(value=value):\n"
                "            self.assertEqual(value, 1)\n"
                "    def test_passes(self):\n"
                "        self.assertEqual(value, 2)\n",
                encoding="utf-8",
            )
            tests = [{"id": f"test_example.py::Cases::{name}"}
                     for name in ("test_subtest", "test_passes")]
            passed, _ = probe(root, tests, {"example.py": "value = 2\n"}, 10)
            self.assertEqual(passed, {"test_example.py::Cases::test_passes"})


if __name__ == "__main__":
    unittest.main()
