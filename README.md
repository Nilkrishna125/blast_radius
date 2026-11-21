# Blast Radius

A selector for the Blast Radius track. It uses coverage maps built from the pinned sqlglot commit, plus AST and fixture analysis, to run only the tests a patch can reach. On the 30 sample commits it scores 118.642 s simulated with 0 misses, and 0 misses on 147 synthetic mutants checked against a full test run.

## Approach

- `map/inv.json.gz`: per-line coverage from one shared pytest run.
- `map/run.json.gz`: per-test *isolated* coverage (each test in its own forked process, traced with `sys.monitoring`), counting only lines run inside function code. Import-time work, cached functions and `setUpClass` are credited to every test that depends on them, not just the first one in a process.
- `map/classes.json`: for each sqlglot class, the tests that instantiated it or a subclass, or called its classmethods. Class-body changes (Expression `arg_types`, dialect Tokenizer/Parser/Generator settings) use it, so inherited settings reach subclass dialects.
- `map/attrs.json.gz`: per-test reads of class attributes and of individual dict keys (each sqlglot class instrumented in an isolated process), plus attributes read while sqlglot imports and dict entries shared between classes by identity (`{**Base.X}` copies). A change to a class attribute or a dict entry selects the tests that read it.
- `map/eqprofile.json`: for every `a is b` / `a is not b` in sqlglot, whether `==` / `!=` ever gave a different answer during the full test suite. A patch that only swaps identity for equality at sites where they always agreed selects nothing.
- `map/fixtures.json` / `map/fixture_readers.json`: fixture stem to the tests that use its content, and to every test that opens the file (traced).
- `base/` is the pinned sqlglot source and tests, used for AST and symbol-reference analysis at run time.
- `selector.py` selects, per changed statement, the tests that ran it. Files whose AST is unchanged (comment-only edits) select nothing. Imports, deletions, renames, syntax errors and unresolved cases fall back to conservative selections.
- Selected tests are scheduled longest-first, choosing the worker count that minimizes `busiest worker + 2 s per worker`.
- `tools/` rebuilds `run.json.gz`, `classes.json` and `fixture_readers.json` from a sqlglot checkout (`tools/build_maps.sh`). Not part of the image.

## Credits

`base/sqlglot` is [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, MIT licensed (see `base/LICENSE`).

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

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

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
