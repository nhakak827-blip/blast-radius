"""Synthetic regression checks for patch interpretation and selection."""
import importlib.util
import ast
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
import sys
import types

from selector import Impact, apply_hunks, optimized_workers, parse_patch, probe, probe_batch
from probe import patch_loaded_functions, collect_for_probes, run_collected_unittest


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

    def test_configuration_reads_preserve_the_observed_readers(self):
        path = 'sqlglot/dialects/example.py'
        self.impact.config_reads = {path: {'WEEK_OFFSET': {'': 2}, 'NUMERIC_ESCAPES': {'x': 1}}}
        klass = ast.parse('class Tokenizer: pass').body[0]
        before = ast.parse('WEEK_OFFSET = -1').body[0]
        after = ast.parse('WEEK_OFFSET = -2').body[0]
        self.assertEqual(self.impact.class_data(path, klass, before, after), 2)
        before = ast.parse("NUMERIC_ESCAPES = {'x': (16, 2, 2, 255), 'u': (16, 4, 4, 65535)}").body[0]
        after = ast.parse("NUMERIC_ESCAPES = {'x': (16, 2, 2, 255), 'u': (17, 4, 4, 65535)}").body[0]
        self.assertEqual(self.impact.class_data(path, klass, before, after), 0)
        self.impact.config_reads[path]['NUMERIC_ESCAPES']['u'] = 1
        self.assertEqual(self.impact.class_data(path, klass, before, after), 1)


class LoadedFunctionTests(unittest.TestCase):
    def test_pending_generators_require_fresh_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = 'def values():\n    yield 1\n'
            (root / 'generator_patch_fixture.py').write_text(source, encoding='utf-8')
            module = types.ModuleType('generator_patch_fixture')
            exec(compile(source, str(root / 'generator_patch_fixture.py'), 'exec'), module.__dict__)
            sys.modules[module.__name__] = module
            self.addCleanup(sys.modules.pop, module.__name__, None)
            pending = module.values()
            self.assertFalse(patch_loaded_functions(root, {'generator_patch_fixture.py':
                source.replace('yield 1', 'yield 2')}))
            self.assertEqual(next(pending), 1)

    def test_cached_decorators_require_fresh_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = 'from functools import lru_cache\n@lru_cache(None)\ndef value():\n    return 1\n'
            (root / 'cached_patch_fixture.py').write_text(source, encoding='utf-8')
            module = types.ModuleType('cached_patch_fixture')
            exec(compile(source, str(root / 'cached_patch_fixture.py'), 'exec'), module.__dict__)
            sys.modules[module.__name__] = module
            self.addCleanup(sys.modules.pop, module.__name__, None)
            self.assertEqual(module.value(), 1)
            self.assertFalse(patch_loaded_functions(root, {'cached_patch_fixture.py':
                source.replace('return 1', 'return 2')}))
            self.assertEqual(module.value(), 1)

    def test_aliases_observe_body_changes_and_signature_changes_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = 'def value():\n    return 1\n'
            (root / 'hot_patch_fixture.py').write_text(source, encoding='utf-8')
            module = types.ModuleType('hot_patch_fixture')
            exec(compile(source, str(root / 'hot_patch_fixture.py'), 'exec'), module.__dict__)
            sys.modules[module.__name__] = module
            self.addCleanup(sys.modules.pop, module.__name__, None)
            alias = module.value
            self.assertTrue(patch_loaded_functions(root, {'hot_patch_fixture.py':
                'def value():\n    return 2\n'}))
            self.assertEqual(alias(), 2)
            self.assertFalse(patch_loaded_functions(root, {'hot_patch_fixture.py':
                'def value():\n    return 3\n'}, {id(alias.__code__)}))
            self.assertEqual(alias(), 2)
            self.assertFalse(patch_loaded_functions(root, {'hot_patch_fixture.py':
                'def value(argument=3):\n    return argument\n'}))
            self.assertEqual(alias(), 2)


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
    def test_collected_unittest_preserves_subtests_skips_and_class_cleanup(self):
        name = 'collected_unittest_fixture'
        module = types.ModuleType(name)
        exec('import unittest\n'
             'events = []\n'
             'class Cases(unittest.TestCase):\n'
             '    @classmethod\n'
             '    def setUpClass(cls):\n'
             '        events.append("setup")\n'
             '        cls.addClassCleanup(events.append, "cleanup")\n'
             '    def tearDown(self):\n'
             '        events.append("teardown")\n'
             '    def test_fail(self):\n'
             '        with self.subTest("broken"):\n'
             '            self.assertEqual(1, 2)\n'
             '    def test_skip(self):\n'
             '        with self.subTest("skipped"):\n'
             '            self.skipTest("keep candidate")\n'
             '    def test_pass(self):\n'
             '        self.assertTrue(True)\n', module.__dict__)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        ids = [f'{name}.py::Cases::test_{case}' for case in ('fail', 'skip', 'pass')]
        self.assertEqual(run_collected_unittest({'ids': ids}), [ids[-1]])
        self.assertEqual(module.events, ['setup', 'teardown', 'teardown', 'teardown', 'cleanup'])
        module.events.clear()
        self.assertEqual(run_collected_unittest({'ids': ids, 'stop_on_failure': True}), [])
        self.assertEqual(module.events, ['setup', 'teardown', 'cleanup'])
        def broken_cleanup():
            raise RuntimeError('class cleanup failed')
        module.Cases.tearDownClass = staticmethod(broken_cleanup)
        self.assertEqual(run_collected_unittest({'ids': [ids[-1]]}), [])
        del module.Cases.tearDownClass
        module.Cases.pytestmark = ['custom fixture semantics']
        self.assertIsNone(run_collected_unittest({'ids': ids}))

    def test_collection_records_import_calls_and_releases_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            filename = root / 'test_collect_probe_fixture.py'
            filename.write_text('def initialize():\n    return 1\nvalue = initialize()\n'
                                'def test_value():\n    assert value == 1\n', encoding='utf-8')
            monitoring = getattr(sys, 'monitoring', None)
            before = [monitoring.get_tool(tool) for tool in (3, 4, 5)] if monitoring else None
            with redirect_stdout(StringIO()):
                status, calls = collect_for_probes(root, [str(filename)])
            self.assertEqual(status, 0)
            module = sys.modules['test_collect_probe_fixture']
            self.addCleanup(sys.modules.pop, 'test_collect_probe_fixture', None)
            self.assertIn(id(module.initialize.__code__), calls)
            if monitoring:
                self.assertEqual([monitoring.get_tool(tool) for tool in (3, 4, 5)], before)

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
            if all(importlib.util.find_spec(name) for name in ("numpy", "pandas", "duckdb")):
                results, _ = probe_batch(root, [
                    {"ids": [test["id"] for test in reversed(tests)], "sources": {"example.py": "value = 2\n"}},
                    {"ids": [test["id"] for test in tests], "sources": {"example.py": "value = 1\n"}},
                    {"ids": [test["id"] for test in tests], "sources": {"example.py": "value = 2\n"}},
                    {"ids": [test["id"] for test in tests], "sources": {"example.py": "value = 2\n"},
                     'milliseconds': 100},
                ])
                self.assertEqual(results, [{"test_example.py::Cases::test_passes"},
                                           {"test_example.py::Cases::test_subtest"}, set(),
                                           {"test_example.py::Cases::test_passes"}])


if __name__ == "__main__":
    unittest.main()
