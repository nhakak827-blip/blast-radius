"""Benchmark the starter, coverage-only selection, and bounded verification."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

from score import evaluate_run, run_total_microseconds
from selector import optimized_workers


def starter(tests, commits):
    loads = [0.0] * 3
    workers = [[], [], []]
    for test in sorted(tests, key=lambda item: item["seconds"], reverse=True):
        worker = loads.index(min(loads))
        loads[worker] += test["seconds"]
        workers[worker].append(test["number"])
    return "".join(json.dumps({"commit": commit, "workers": workers}) + "\n"
                   for commit in sorted(commits)).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Benchmark a built Linux container")
    parser.add_argument("--checkout", type=Path, default=Path(".sqlglot"))
    parser.add_argument("--index", type=Path, default=Path("impact.json.gz"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--target-seconds", type=Decimal, default=Decimal('100.0'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sample = root / "sample"
    tests = json.loads((sample / "tests.json").read_text())["tests"]
    answers = json.loads((sample / "answers.json").read_text())["commits"]
    print(f"Starter: {evaluate_run(starter(tests, answers), tests, answers)}", flush=True)
    durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    reference = [
        json.dumps({"commit": commit, "workers": optimized_workers(answer["failing"], durations)})
        for commit, answer in sorted(answers.items())
    ]
    print(f"Known-failures reference: {evaluate_run(chr(10).join(reference).encode(), tests, answers)}",
          flush=True)

    if args.image:
        # Measure after selector shutdown so its interpreter cleanup is included.
        container_runner = """
import pathlib
import resource
import subprocess
import sys
result = subprocess.run([sys.executable, '/app/selector.py', *sys.argv[1:]])
try:
    stats = dict(line.split() for line in pathlib.Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
    cpu = int(stats['usage_usec']) / 1_000_000
except (OSError, KeyError, ValueError):
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime
print(f'Final CPU seconds: {cpu:.6f}', file=sys.stderr)
sys.exit(result.returncode)
"""
        command = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
            "--mount", f"type=bind,source={sample / 'commits'},target=/data/commits,readonly",
            "--mount", f"type=bind,source={sample / 'tests.json'},target=/data/tests.json,readonly",
            '--entrypoint', 'python', args.image, '-c', container_runner,
        ]
    else:
        command = [sys.executable, str(root / "selector.py"), "--data", str(sample),
                   "--checkout", str(args.checkout.resolve()), "--index", str(args.index.resolve())]

    modes = ["coverage"] if args.coverage_only else ["coverage", "verified"]
    for mode in modes:
        totals = []
        estimates = []
        for run in range(1, args.runs + 1):
            options = ["--coverage-only"] if mode == "coverage" else []
            start = time.monotonic()
            result = subprocess.run(command + options + ["--report-cpu"], capture_output=True,
                                    check=True, timeout=180)
            wall = time.monotonic() - start
            evaluation = evaluate_run(result.stdout, tests, answers)
            if not evaluation["ok"] or len(result.stdout) > 2_000_000:
                raise SystemExit(f"{mode} rejected: {evaluation}")
            match = re.search(rb"Final CPU seconds: ([\d.]+)", result.stderr)
            if match is None:
                match = re.search(rb"CPU seconds: ([\d.]+)", result.stderr)
            cpu = float(match[1]) if match else None
            estimate = re.search(rb"CPU estimate(?: \(partial\))?: ([\d.]+)", result.stderr)
            simulated = evaluation["simulated_milliseconds"]
            diagnostics = re.search(rb"^Probe CPU seconds:.*$", result.stderr, re.MULTILINE)
            if diagnostics:
                print(diagnostics[0].decode(), flush=True)
            report = (f"{mode} run {run}: simulated={simulated / 1000:.3f}s "
                      f"wall={wall:.3f}s CPU={cpu} "
                      f"{estimate[0].decode() + 's ' if estimate else ''}"
                      f"misses={evaluation['missed_commits']}")
            print(report, flush=True)
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                print(f'::notice title=Sample benchmark::{report}', flush=True)
            if evaluation['missed_commits']:
                raise SystemExit(f'{mode} missed failing tests')
            if cpu is not None:
                totals.append(run_total_microseconds(simulated, round(cpu * 1_000_000)))
            elif estimate and b"partial" not in estimate[0]:
                estimates.append(run_total_microseconds(simulated, round(float(estimate[1]) * 1_000_000)))
            (root / f"{mode}.jsonl").write_bytes(result.stdout)
        if totals:
            median = int(statistics.median(totals))
            print(f"{mode} median total: {median / 1_000_000:.6f}s "
                  f"(rounded {(median + 50_000) // 100_000 / 10:.1f}s)", flush=True)
            if args.image and mode == 'verified':
                score = (Decimal(median) / Decimal(1_000_000)).quantize(
                    Decimal('0.1'), rounding=ROUND_HALF_UP)
                if score >= args.target_seconds:
                    message = f'Total {score}s exceeds the target below {args.target_seconds}s'
                    if os.environ.get('GITHUB_ACTIONS') == 'true':
                        print(f'::error title=Score target missed::{message}', flush=True)
                    raise SystemExit(message)
                print(f'Target passed: {score}s below {args.target_seconds}s', flush=True)
        elif len(estimates) == args.runs:
            median = statistics.median(estimates)
            print(f"{mode} median estimated total: {median / 1_000_000:.6f}s", flush=True)


if __name__ == "__main__":
    main()
