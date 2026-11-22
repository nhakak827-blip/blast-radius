"""Collect a dependency index from the clean SQLGlot checkout."""
import argparse
import ast
import gzip
import json
import os
from collections import defaultdict
from pathlib import Path
import sys


def build(root, output):
    import coverage
    import pytest
    import build_trace
    from inline_pool import install

    install()
    build_trace.install(root)

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["COVERAGE_CORE"] = "ctrace"

    tracer = coverage.Coverage(source=["sqlglot", "tests"], data_file=None)
    reads = defaultdict(set)
    scope_members = defaultdict(set)
    prefix = os.path.normcase(str(root)) + os.sep
    active = ""

    def audit(event, args):
        if event != "open" or not isinstance(args[0], (str, bytes)):
            return
        path = os.path.normcase(os.fsdecode(args[0]))
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(prefix, path))
        if path.startswith(prefix):
            relative = path[len(prefix):].replace(os.sep, "/")
            if relative.startswith(("tests/", "sqlglot/")) and not relative.endswith((".py", ".pyc")):
                reads[relative].add(active)

    class Collector:
        def pytest_collection_finish(self, session):
            self.ids = {item.nodeid for item in session.items}
            for item in session.items:
                for scope in item.listchain():
                    scope_members[scope.nodeid].add(item.nodeid)

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item):
            nonlocal active
            active = item.nodeid
            build_trace.context = active
            tracer.switch_context(active)
            try:
                return (yield)
            finally:
                active = ""
                build_trace.context = active
                tracer.switch_context("")

        @pytest.hookimpl(wrapper=True)
        def pytest_fixture_setup(self, fixturedef, request):
            nonlocal active
            if fixturedef.scope == "function":
                return (yield)
            previous = active
            active = "fixture:" + request.node.nodeid
            build_trace.context = active
            tracer.switch_context(active)
            try:
                return (yield)
            finally:
                active = previous
                build_trace.context = active
                tracer.switch_context(active)

    plugin = Collector()
    sys.addaudithook(audit)
    tracer.start()
    try:
        result = pytest.main(["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
                              "tests"], plugins=[plugin])
    finally:
        tracer.stop()
    if result != 0:
        raise RuntimeError(f"Baseline pytest exit {result}; see test log")
    if not getattr(plugin, "ids", None):
        raise RuntimeError("No baseline tests collected")

    data = tracer.get_data()
    ids = sorted(plugin.ids)
    positions = {nodeid: 1 << index for index, nodeid in enumerate(ids)}
    for scope, members in scope_members.items():
        positions["fixture:" + scope] = sum(positions[member] for member in members)
    masks = [0]
    mask_ids = {0: 0}

    def intern(contexts):
        mask = 0
        for context in contexts:
            mask |= positions.get(context, 0)
        if mask not in mask_ids:
            mask_ids[mask] = len(masks)
            masks.append(mask)
        return mask_ids[mask]

    files = {}
    for name in sorted(data.measured_files()):
        path = Path(name).resolve().relative_to(root).as_posix()
        lines = data.contexts_by_lineno(name)
        files[path] = {str(line): intern(contexts) for line, contexts in sorted(lines.items())}
    fixtures = {name: intern(contexts) if "" not in contexts else None
                for name, contexts in sorted(reads.items())}
    config_reads = {}
    for (path, name, key), contexts in sorted(build_trace.reads.items()):
        config_reads.setdefault(path, {}).setdefault(name, {})[key] = (
            None if '' in contexts else intern(contexts))
    index = {"ids": ids, "files": files, "fixtures": fixtures,
             "masks": [format(mask, "x") for mask in masks], 'config_reads': config_reads}
    add_static_analysis(root, index)
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
            compressed.write(json.dumps(index, separators=(",", ":")).encode())


def add_static_analysis(root, index):
    # Static references and type proofs depend only on the clean pinned checkout.
    # Building them here avoids scanning the entire source tree for each runtime batch.
    from comparisons import comparison_proofs
    masks = [int(mask, 16) for mask in index["masks"]]
    corpus = {}
    for folder in ("sqlglot", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            corpus[path.relative_to(root).as_posix()] = source, ast.parse(source, type_comments=True)
    references = defaultdict(int)
    proofs = {}
    for path, (_, tree) in corpus.items():
        lines = index["files"].get(path, {})
        for node in ast.walk(tree):
            symbol = None
            if isinstance(node, ast.Name):
                symbol = node.id
            elif isinstance(node, ast.Attribute):
                symbol = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                symbol = node.value
            if symbol is not None:
                for line in range(node.lineno, node.end_lineno + 1):
                    references[symbol] |= masks[lines.get(str(line), 0)]
        if path.startswith("sqlglot/"):
            proven = comparison_proofs(root, tree, corpus)
            if proven:
                proofs[path] = proven
    index["references"] = {symbol: format(mask, "x") for symbol, mask in sorted(references.items())}
    index["comparison_proofs"] = proofs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.checkout.resolve(), args.output.resolve())
