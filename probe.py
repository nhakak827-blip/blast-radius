"""Run a bounded pytest probe in a fresh child and keep inconclusive test IDs."""
import importlib.machinery
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time


def run(checkout, request):
    import pytest
    from inline_pool import install

    install()
    root = checkout.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    edits = {str(root / path): source for path, source in request["sources"].items()}
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
             "--timeout=4", *request["ids"]],
            plugins=[reports],
        )
    finally:
        importlib.machinery.SourceFileLoader.get_code = get_code
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return []
    return sorted(reports.completed - reports.failed)


def batch_child(checkout, request, connection):
    """Keep each patch's imports, globals and pytest state inside its own process."""
    sys.stdout = sys.stderr
    try:
        passing = run(checkout, request)
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
        process = context.Process(target=batch_child, args=(checkout, request, sender))
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
