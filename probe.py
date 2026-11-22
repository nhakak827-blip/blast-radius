"""Run a bounded pytest probe in a fresh child and keep inconclusive test IDs."""
import importlib.machinery
import json
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


if __name__ == "__main__":
    request = json.load(sys.stdin)
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    passed = run(Path(sys.argv[1]), request)
    print(json.dumps({"passing": passed, "cpu_seconds": time.process_time()}), file=saved_stdout)
