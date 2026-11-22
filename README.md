# Blast Radius selector

The image builds a test dependency index from the clean, pinned SQLGlot checkout. The selector uses that index and the incoming diffs to choose tests, then evaluates longest-processing-time schedules with up to eight workers using the scorer's integer-millisecond model.

The build records coverage by pytest test ID and observes fixture reads. Shared fixtures are attributed to their scope, and SQLGlot's process-pool computations run inline during tracing so they can be attributed to the test that submitted them. At runtime, an AST comparison handles edits to executable statements and scope changes. The selector falls back to broader impact sets when the diff, syntax, or dependency is ambiguous. For small Python-only selections, an isolated pytest probe can remove confirmed passing candidates. Incomplete results, subtest failures, and timeouts leave the candidates selected.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.12 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
make test     # synthetic diff, impact, scheduling, and probe checks
make benchmark # three runs per mode with scorer limits and CPU reporting
```

`benchmark.py` compares the original three-worker starter, coverage-only selection, and selection with runtime verification. Its known-failures reference is an offline comparison using the provided sample answers. Only the benchmark and scorer read those answers. The image builds its index from upstream source and receives only patches and the test inventory at runtime.

Use Python 3.13 for the closest match when benchmarking without Docker.

```sh
git init .sqlglot
git -C .sqlglot fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa
git -C .sqlglot checkout --detach FETCH_HEAD
python -m pip install -r requirements.lock coverage==7.17.4
python build_index.py .sqlglot impact.json.gz
python -m unittest -v test_selector
python benchmark.py
```

The local benchmark validates the same output format and simulated score. Use the Docker benchmark to compare Linux container CPU and the three-run median. Runtime probing and in-process coverage of worker computations do not reproduce process behavior such as serialization and timing.

### Sample results

The 30-commit benchmark ran on Windows with Python 3.12 and the locked dependencies. Every selection produced valid scorer output with zero misses.

| Mode | Simulated score | Median with estimated local CPU |
| --- | ---: | ---: |
| Starter | 1,210.470 s | |
| Coverage only | 113.067 s | 113.364 s |
| Coverage and verification | 94.351 s | 98.039 s |

The verified mode matched the 94.351 s reference obtained by scheduling only the known sample failures. Local CPU estimates cover the selector and completed probes. The Docker build and Linux container score remain unverified.

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Submit

Run `make test` and `make benchmark`, then push the solution to a public GitHub repository with the `Dockerfile` at its root. Enter the repository and exact commit hash on the track page. Compare the practice score before choosing the final commit. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
