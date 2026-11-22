"""Run a bounded pytest probe in a fresh child and keep inconclusive test IDs."""
import importlib.machinery
import ast
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time

from comparisons import identical


def patch_loaded_functions(root, sources, collected_calls=None):
    """Preserve all existing aliases when replacing an ordinary function body."""
    changes = []
    for path, source in sources.items():
        module_name = path.removesuffix('.py').replace('/', '.')
        module_name = module_name.removesuffix('.__init__')
        module = sys.modules.get(module_name)
        if module is None:
            continue
        original = ast.parse((root / path).read_text(encoding='utf-8'), type_comments=True)
        updated = ast.parse(source, type_comments=True)
        compiled = compile(source, str(root / path), 'exec', dont_inherit=True)
        codes = {}
        pending = [compiled]
        while pending:
            code = pending.pop()
            codes[code.co_qualname] = code
            pending.extend(value for value in code.co_consts if inspect.iscode(value))

        def visit(before, after, owner, prefix):
            if identical(before, after):
                return True
            if type(before) is not type(after):
                return False
            if isinstance(before, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(not identical(getattr(before, name, None), getattr(after, name, None))
                       for name in before._fields if name != 'body'):
                    return False
                if any(not isinstance(decorator, ast.Name)
                       or decorator.id not in {'staticmethod', 'classmethod', 'property'}
                       for decorator in before.decorator_list):
                    return False
                target = vars(owner).get(before.name)
                if isinstance(target, (classmethod, staticmethod)):
                    target = target.__func__
                elif isinstance(target, property):
                    target = target.fget
                if target is None:
                    return False
                target = inspect.unwrap(target)
                name = prefix + before.name
                code = codes.get(name)
                if (not inspect.isfunction(target) or code is None
                        or target.__code__.co_qualname != name
                        or Path(target.__code__.co_filename).resolve() != (root / path).resolve()
                        or target.__code__.co_freevars != code.co_freevars):
                    return False
                if collected_calls is not None and id(target.__code__) in collected_calls:
                    return False
                changes.append((target, code))
                return True
            if not isinstance(before, (ast.Module, ast.ClassDef)):
                return False
            if any(not identical(getattr(before, name, None), getattr(after, name, None))
                   for name in before._fields if name != 'body'):
                return False
            if len(before.body) != len(after.body):
                return False
            if isinstance(before, ast.ClassDef):
                owner = vars(owner).get(before.name)
                if not isinstance(owner, type):
                    return False
                prefix += before.name + '.'
            return all(visit(a, b, owner, prefix) for a, b in zip(before.body, after.body))

        if not visit(original, updated, module, ''):
            return False
    # Apply only after every module has passed validation.
    for function, code in changes:
        function.__code__ = code
    return True


def clear_checkout_modules(root):
    for name, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if filename and Path(filename).resolve().is_relative_to(root):
            del sys.modules[name]


def run(checkout, request, warmed=False, collected_calls=None):
    import pytest
    from inline_pool import install

    install()
    root = checkout.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    edits = {str(root / path): source for path, source in request["sources"].items()}
    if warmed and not patch_loaded_functions(root, request['sources'], collected_calls):
        clear_checkout_modules(root)
    get_code = importlib.machinery.SourceFileLoader.get_code

    def patched_code(loader, fullname):
        source = edits.get(os.path.normpath(loader.path))
        if source is None:
            return get_code(loader, fullname)
        return compile(source, loader.path, "exec", dont_inherit=True)

    class Reports:
        def __init__(self):
            self.completed = set()
            self.failed = set()

        def pytest_runtest_logreport(self, report):
            if report.failed or report.skipped:
                self.failed.add(report.nodeid)
            if report.when == "teardown" and report.passed:
                self.completed.add(report.nodeid)

    reports = Reports()
    importlib.machinery.SourceFileLoader.get_code = patched_code
    try:
        status = pytest.main(
            ["-q", "--tb=no", "-p", "no:cacheprovider", "-p", "no:assertion", "-p", "pytest_timeout",
             "--timeout=4", *(['-x'] if request.get('stop_on_failure') else []), *request["ids"]],
            plugins=[reports],
        )
    finally:
        importlib.machinery.SourceFileLoader.get_code = get_code
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return []
    return sorted(reports.completed - reports.failed)


def batch_child(checkout, request, connection, warmed, collected_calls):
    """Keep each patch's imports, globals and pytest state inside its own process."""
    sys.stdout = sys.stderr
    try:
        passing = run(checkout, {**request, 'stop_on_failure': True}, warmed, collected_calls)
        connection.send({"passing": passing, "cpu_seconds": time.process_time()})
    finally:
        connection.close()


def run_batch(checkout, requests):
    # Forked probes share the expensive clean imports, while SQLGlot and the
    # tests are first imported after forking and applying the individual patch.
    import pytest
    import pytest_timeout
    import numpy
    import pandas
    import duckdb
    from inline_pool import install

    install()
    warmed = hasattr(os, 'fork')
    collected_calls = set()
    if warmed:
        os.environ['SKIP_INTEGRATION'] = '0'
        os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        root = checkout.resolve()
        os.chdir(root)
        sys.path.insert(0, str(root))
        # Collection imports the clean modules without executing any test bodies.
        targets = ['tests'] if (root / 'tests').is_dir() else list(dict.fromkeys(
            nodeid for request in requests for nodeid in request['ids']))
        prefix = str(root) + os.sep

        def observe_calls(frame, event, argument):
            if event == 'call' and frame.f_code.co_filename.startswith(prefix):
                collected_calls.add(id(frame.f_code))

        previous_profile = sys.getprofile()
        sys.setprofile(observe_calls)
        try:
            status = pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider',
                                  '-p', 'no:assertion', *targets])
        finally:
            sys.setprofile(previous_profile)
        if status != pytest.ExitCode.OK:
            clear_checkout_modules(root)
            warmed = False
    context = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
    deadline = time.monotonic() + 70
    results = []
    timings = []
    child_cpu = 0.0
    complete = True
    for request in requests:
        remaining = deadline - time.monotonic()
        if remaining < 2:
            results.append([])
            timings.append(None)
            continue
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=batch_child,
                                  args=(checkout, request, sender, warmed, collected_calls))
        try:
            process.start()
            sender.close()
            if receiver.poll(min(15, remaining)):
                result = receiver.recv()
                results.append(result["passing"])
                timings.append(result["cpu_seconds"])
                child_cpu += result["cpu_seconds"]
            else:
                results.append([])
                timings.append(None)
                complete = False
        except (EOFError, OSError):
            results.append([])
            timings.append(None)
            complete = False
        finally:
            if process.pid is not None:
                process.join(timeout=0.1)
                if process.is_alive():
                    process.terminate()
                    process.join()
            receiver.close()
            sender.close()
    return {"passing": results, "cpu_seconds": time.process_time() + child_cpu,
            "complete": complete, "probe_seconds": timings}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    if "--batch" in sys.argv[2:]:
        result = run_batch(Path(sys.argv[1]), request)
    else:
        passed = run(Path(sys.argv[1]), request)
        result = {"passing": passed, "cpu_seconds": time.process_time()}
    print(json.dumps(result), file=saved_stdout)
