"""Select impacted SQLGlot tests and minimize integer-millisecond worker cost."""
import argparse
import ast
from dataclasses import dataclass, field
import gzip
from heapq import heapify, heapreplace
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from comparisons import equivalent_comparison

DATA = Path("/data")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
PATH = re.compile(r"[\w./-]+", re.ASCII)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
IMPORTS = (ast.Import, ast.ImportFrom)


@dataclass
class Hunk:
    start: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


@dataclass
class Edit:
    before: str
    after: str
    hunks: list[Hunk]


def patch_path(line, prefix):
    name = line[4:].split("\t", 1)[0]
    if name == "/dev/null":
        return ""
    path = name.removeprefix(prefix)
    if not name.startswith(prefix) or not PATH.fullmatch(path) or ".." in path.split("/"):
        raise ValueError("Unsupported path")
    return path


def parse_patch(text):
    lines = text.splitlines()
    edits = []
    index = 0
    pending_git_header = False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("diff --git "):
            if pending_git_header:
                raise ValueError("Git file record has no hunks")
            pending_git_header = True
            continue
        if pending_git_header and line.startswith(("index ", "new file mode ", "deleted file mode ")):
            continue
        if not line.startswith("--- ") or index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("Unsupported patch record")
        edit = Edit(patch_path(line, "a/"), patch_path(lines[index], "b/"), [])
        index += 1
        while index < len(lines) and lines[index].startswith("@@"):
            match = HUNK.match(lines[index])
            if not match:
                raise ValueError("Invalid hunk header")
            old_start, old_count, new_start, new_count = match.groups()
            old_count = int(old_count or "1")
            new_count = int(new_count or "1")
            hunk = Hunk(int(old_start) - bool(old_count))
            shift = sum(len(previous.new) - len(previous.old) for previous in edit.hunks)
            if int(new_start) - bool(new_count) != hunk.start + shift:
                raise ValueError("Inconsistent hunk position")
            index += 1
            while len(hunk.old) < old_count or len(hunk.new) < new_count:
                if index >= len(lines):
                    raise ValueError("Truncated hunk")
                record = lines[index]
                index += 1
                if record == r"\ No newline at end of file":
                    continue
                tag = record[:1]
                if tag not in (" ", "-", "+"):
                    raise ValueError("Invalid hunk body")
                if tag != "+":
                    hunk.old.append(record[1:])
                if tag != "-":
                    hunk.new.append(record[1:])
            if (len(hunk.old), len(hunk.new)) != (old_count, new_count):
                raise ValueError("Invalid hunk counts")
            if index < len(lines) and lines[index] == r"\ No newline at end of file":
                index += 1
            edit.hunks.append(hunk)
        if not edit.hunks:
            raise ValueError("Missing hunks")
        edits.append(edit)
        pending_git_header = False
    if not edits or pending_git_header:
        raise ValueError("Incomplete patch")
    return edits


def apply_hunks(source, hunks):
    original = source.splitlines()
    updated = []
    offset = 0
    for hunk in hunks:
        end = hunk.start + len(hunk.old)
        if hunk.start < offset or end > len(original) or original[hunk.start:end] != hunk.old:
            raise ValueError("Hunk does not match the pinned source")
        updated.extend(original[offset:hunk.start])
        updated.extend(hunk.new)
        offset = end
    updated.extend(original[offset:])
    return "\n".join(updated) + "\n"


def optimized_workers(numbers, durations):
    order = sorted(numbers, key=lambda number: (-durations[number], number))
    best_cost = math.inf
    best = []
    for count in range(1, min(8, len(order)) + 1):
        workers = [[] for _ in range(count)]
        loads = [(0, worker) for worker in range(count)]
        for worker, number in enumerate(order[:count]):
            workers[worker].append(number)
            loads[worker] = (durations[number], worker)
        heapify(loads)
        for number in order[count:]:
            load, worker = loads[0]
            workers[worker].append(number)
            heapreplace(loads, (load + durations[number], worker))
        cost = max(load for load, _ in loads) + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, workers
    return best


def identical(before, after):
    return ast.dump(before) == ast.dump(after)


def executable_changes(before, after):
    """Locate old statements that can observe body-only edits."""
    if identical(before, after):
        return []
    if type(before) is not type(after):
        return [before]
    locations = []
    for field_name, old_value in ast.iter_fields(before):
        new_value = getattr(after, field_name)
        if isinstance(old_value, list) and old_value and all(isinstance(node, ast.stmt) for node in old_value):
            if len(old_value) != len(new_value):
                return [before]
            for old_node, new_node in zip(old_value, new_value):
                locations.extend(executable_changes(old_node, new_node))
        elif isinstance(old_value, ast.AST) and isinstance(new_value, ast.AST):
            if not identical(old_value, new_value):
                return [before]
        elif isinstance(old_value, list):
            if [ast.dump(x) if isinstance(x, ast.AST) else x for x in old_value] != [
                ast.dump(x) if isinstance(x, ast.AST) else x for x in new_value
            ]:
                return [before]
        elif old_value != new_value:
            return [before]
    return locations


class Impact:
    def __init__(self, index, checkout, tests):
        self.checkout = checkout
        self.tests = tests
        self.positions = {nodeid: position for position, nodeid in enumerate(index["ids"])}
        self.files = index["files"]
        self.fixtures = index["fixtures"]
        self.masks = [int(mask, 16) for mask in index["masks"]]
        self.all = (1 << len(self.positions)) - 1
        self.file_masks = {}
        for path, lines in self.files.items():
            self.file_masks[path] = self.union(lines.values())
        self.sources = {}
        self.symbols = {}

    def union(self, mask_ids):
        mask = 0
        for mask_id in mask_ids:
            mask |= self.masks[mask_id]
        return mask

    def lines(self, path, start, end):
        lines = self.files.get(path, {})
        return self.union(lines.get(str(line), 0) for line in range(start, end + 1))

    def source(self, path):
        if path not in self.sources:
            source = (self.checkout / path).read_text(encoding="utf-8")
            self.sources[path] = source, ast.parse(source, filename=path, type_comments=True)
        return self.sources[path]

    def component_mask(self, path):
        parts = path.split("/")
        if len(parts) == 3 and parts[1] in {"dialects", "generators", "parsers", "tokenizers", "typing"}:
            name = parts[2]
            if name not in {"dialect.py", "__init__.py"}:
                return self.union_for_component(name)
        return self.all

    def union_for_component(self, filename):
        mask = 0
        for folder in ("dialects", "generators", "parsers", "tokenizers", "typing"):
            mask |= self.file_masks.get(f"sqlglot/{folder}/{filename}", 0)
        return mask

    def references(self, symbol):
        """Join statically named references to the tests that executed them."""
        if symbol not in self.symbols:
            pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
            mask = 0
            for path in self.files:
                source = (self.checkout / path).read_text(encoding="utf-8")
                if not pattern.search(source):
                    continue
                _, tree = self.source(path)
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Name) and node.id == symbol
                        or isinstance(node, ast.Attribute) and node.attr == symbol
                        or isinstance(node, ast.Constant) and node.value == symbol):
                        mask |= self.lines(path, node.lineno, node.end_lineno)
            self.symbols[symbol] = mask
        return self.symbols[symbol]

    def class_data(self, path, klass, before, after):
        if not isinstance(before, ast.Assign) or not isinstance(after, ast.Assign):
            return None
        if len(before.targets) != 1 or not isinstance(before.targets[0], ast.Name):
            return None
        if not identical(ast.Tuple(elts=before.targets), ast.Tuple(elts=after.targets)):
            return None
        name = before.targets[0].id
        if path.startswith("sqlglot/dialects/") and name == "WEEK_OFFSET":
            try:
                offsets = [ast.literal_eval(item.value) for item in (before, after)]
            except (ValueError, TypeError):
                return None
            if all(type(offset) is int for offset in offsets):
                return self.references(name)
        if (path.startswith("sqlglot/dialects/") and name == "NUMERIC_ESCAPES"
                and klass.name == "Tokenizer"):
            try:
                old, new = (ast.literal_eval(item.value) for item in (before, after))
            except (ValueError, TypeError):
                return None
            if not isinstance(old, dict) or not isinstance(new, dict) or old.keys() != new.keys():
                return None
            if not all(isinstance(value, tuple) and len(value) == 4
                       and all(type(number) is int for number in value)
                       for value in (*old.values(), *new.values())):
                return None
            # Generator consumers test only key membership or dict truthiness. Tokenizer
            # consumers of the values enter one of the two numeric escape readers.
            engine = "sqlglot/tokenizer_core.py"
            _, tree = self.source(engine)
            readers = {"_read_byte_escape", "_scan_numeric_escape"}
            mask = 0
            for node in ast.walk(tree):
                if isinstance(node, FUNCTIONS) and node.name in readers:
                    mask |= self.lines(engine, node.lineno, node.end_lineno)
            return self.component_mask(path) & mask
        return None

    def changed_scope(self, path, old, new):
        if identical(old, new):
            return 0
        if isinstance(old, IMPORTS) or isinstance(new, IMPORTS):
            return self.all
        if type(old) is not type(new):
            return self.component_mask(path)
        if isinstance(old, FUNCTIONS):
            # Defaults, decorators and signatures affect definition-time behavior and callers.
            headers_equal = all(
                identical(ast.Module(body=[getattr(old, key)], type_ignores=[]),
                          ast.Module(body=[getattr(new, key)], type_ignores=[]))
                for key in ("args", "returns")
            )
            if not headers_equal or old.name != new.name or [ast.dump(x) for x in old.decorator_list] != [
                ast.dump(x) for x in new.decorator_list
            ] or [ast.dump(x) for x in old.type_params] != [ast.dump(x) for x in new.type_params]:
                return self.component_mask(path)
            if len(old.body) != len(new.body) or isinstance(old.body[0], ast.Expr) and isinstance(
                old.body[0].value, ast.Constant
            ) and isinstance(old.body[0].value.value, str) and not identical(old.body[0], new.body[0]):
                return self.lines(path, old.lineno, old.end_lineno)
            mask = 0
            for before, after in zip(old.body, new.body):
                for node in executable_changes(before, after):
                    mask |= self.lines(path, node.lineno, node.end_lineno)
            return mask
        if isinstance(old, (ast.Module, ast.ClassDef)):
            old_imports = [ast.dump(item) for item in old.body if isinstance(item, IMPORTS)]
            new_imports = [ast.dump(item) for item in new.body if isinstance(item, IMPORTS)]
            if old_imports != new_imports:
                return self.all
            if isinstance(old, ast.ClassDef):
                old_header = (old.name, *map(ast.dump, old.bases + old.keywords + old.decorator_list + old.type_params))
                new_header = (new.name, *map(ast.dump, new.bases + new.keywords + new.decorator_list + new.type_params))
                if old_header != new_header:
                    return self.component_mask(path)
                if path.startswith("sqlglot/expressions/") and len(old.body) == len(new.body):
                    changed = [(a, b) for a, b in zip(old.body, new.body) if not identical(a, b)]
                    if all(isinstance(a, ast.Assign) and isinstance(b, ast.Assign)
                           and len(a.targets) == len(b.targets) == 1
                           and isinstance(a.targets[0], ast.Name) and isinstance(b.targets[0], ast.Name)
                           and a.targets[0].id == b.targets[0].id == "arg_types"
                           and isinstance(a.value, ast.Dict) and isinstance(b.value, ast.Dict)
                           for a, b in changed):
                        return self.references(old.name) | self.lines(path, old.lineno, old.end_lineno)
            elif [node.tag for node in old.type_ignores] != [node.tag for node in new.type_ignores]:
                return self.component_mask(path)
            if len(old.body) == len(new.body):
                mask = 0
                for before, after in zip(old.body, new.body):
                    data_mask = (self.class_data(path, old, before, after)
                                 if isinstance(old, ast.ClassDef) and not identical(before, after)
                                 else None)
                    mask |= self.changed_scope(path, before, after) if data_mask is None else data_mask
                return mask
        return self.component_mask(path)

    def change(self, edit, single_file=False):
        path = edit.before or edit.after
        if edit.before and edit.after and edit.before != edit.after:
            return self.all
        if path in self.fixtures:
            mask_id = self.fixtures[path]
            return self.all if mask_id is None else self.masks[mask_id]
        if not edit.before or edit.before != edit.after:
            return self.all
        if path.startswith("tests/"):
            return self.all
        if not path.startswith("sqlglot/") or not path.endswith(".py") or path not in self.files:
            return self.all
        source, old = self.source(path)
        changed = apply_hunks(source, edit.hunks)
        # Compilation catches errors that ast.parse accepts, such as a top-level return.
        compile(changed, path, "exec", dont_inherit=True)
        new = ast.parse(changed, filename=path, type_comments=True)
        if single_file and equivalent_comparison(self.checkout, old, new):
            return 0
        mask = self.changed_scope(path, old, new)
        if mask and path == "sqlglot/serde.py":
            # SQLGlot expression reducers use serde when multiprocessing pickles
            # submitted arguments and returned expressions. Inline tracing skips that boundary.
            mask |= self.references("submit")
        return mask

    def select(self, patch):
        mixed_inputs = False
        sources = {}
        try:
            mask = 0
            edits = parse_patch(patch.read_text(encoding="utf-8"))
            for edit in edits:
                changed = self.change(edit, single_file=len(edits) == 1)
                mask |= changed
                if edit.before == edit.after and edit.before.startswith("sqlglot/") and edit.before.endswith(".py"):
                    source, _ = self.source(edit.before)
                    sources[edit.before] = apply_hunks(source, edit.hunks)
                else:
                    mixed_inputs = True
        except (ValueError, SyntaxError, OSError, RecursionError):
            mask = self.all
        if mask == self.all:
            return self.tests, {}, []
        selected = []
        probeable = []
        for test in self.tests:
            position = self.positions.get(test["id"])
            if position is None:
                selected.append(test)
            elif mask & (1 << position):
                selected.append(test)
                if not mixed_inputs:
                    probeable.append(test)
        return selected, sources, probeable


def probe(checkout, tests, sources, timeout):
    request = {"ids": [test["id"] for test in tests], "sources": sources}
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("probe.py")), str(checkout)],
            input=json.dumps(request), encoding="utf-8", stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=True,
        )
        payload = json.loads(result.stdout)
        return set(payload["passing"]), float(payload["cpu_seconds"])
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        return set(), None


def report_cpu(probe_cpu, probes_complete):
    if os.environ.get("BLAST_RADIUS_CONTAINER") == "1":
        try:
            stats = dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())
            print(f"CPU seconds: {int(stats['usage_usec']) / 1_000_000:.6f}", file=sys.stderr)
            return
        except (OSError, KeyError, ValueError):
            pass
    try:
        import resource
    except ImportError:
        label = "CPU estimate" if probes_complete else "CPU estimate (partial)"
        print(f"{label}: {time.process_time() + probe_cpu:.6f}", file=sys.stderr)
    else:
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(f"CPU seconds: {time.process_time() + children.ru_utime + children.ru_stime:.6f}",
              file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--checkout", type=Path, default=Path("/sqlglot"))
    parser.add_argument("--index", type=Path, default=Path("/app/impact.json.gz"))
    parser.add_argument("--report-cpu", action="store_true")
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--probe-budget-ms", type=int, default=6000)
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    with gzip.open(args.index, "rt", encoding="utf-8") as stream:
        impact = Impact(json.load(stream), args.checkout, tests)
    durations = {test["number"]: math.floor(test["seconds"] * 1000 + 0.5) for test in tests}
    deadline = time.monotonic() + 70
    probe_cpu = 0.0
    probes_complete = True
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected, sources, probeable = impact.select(patch)
        probe_ms = sum(durations[test["number"]] for test in probeable)
        remaining = deadline - time.monotonic()
        if (not args.coverage_only and sources and probeable
                and probe_ms <= args.probe_budget_ms and remaining >= 2):
            passing, cpu = probe(args.checkout, probeable, sources, min(15, remaining))
            if cpu is None:
                probes_complete = False
            else:
                probe_cpu += cpu
            selected = [test for test in selected if test["id"] not in passing]
        workers = optimized_workers([test["number"] for test in selected], durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    if args.report_cpu:
        report_cpu(probe_cpu, probes_complete)


if __name__ == "__main__":
    main()
