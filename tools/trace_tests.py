"""Per-test isolated coverage for sqlglot at the pinned commit.

Every test runs in its own freshly forked child of a parent that has NOT imported sqlglot,
so module-level code, class bodies, metaclass work, lru_cache fills and setUpClass are
credited to every test that depends on them (not just the first test in a process).
Each child also records which files under tests/fixtures it opens.

usage (inside the labeling image, cwd = sqlglot checkout):
  python trace_tests.py tests.json out_dir [jobs] [first] [last]
"""
import json, os, sys, time

TESTS_JSON, OUT = sys.argv[1], sys.argv[2]
JOBS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
tests = json.load(open(TESTS_JSON))["tests"]
lo = int(sys.argv[4]) if len(sys.argv) > 4 else 0
hi = int(sys.argv[5]) if len(sys.argv) > 5 else len(tests)
os.makedirs(OUT, exist_ok=True)
ROOT = os.getcwd()
os.environ["SKIP_INTEGRATION"] = "0"
os.environ.setdefault("COVERAGE_CORE", "sysmon")
sys.path.insert(0, ROOT)




def child(test):
    import builtins, io, importlib, unittest
    import concurrent.futures as cf

    # Process pools would hide work from coverage; run their jobs inline instead.
    class SyncExecutor:
        def __init__(self, *a, initializer=None, initargs=(), **k):
            if initializer is not None:
                initializer(*initargs)
        def submit(self, fn, *args, **kwargs):
            import pickle
            f = cf.Future()
            try:
                args = pickle.loads(pickle.dumps(args))
                kwargs = pickle.loads(pickle.dumps(kwargs))
                f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as e:
                f.set_exception(e)
            return f
        def map(self, fn, *its, **k):
            return map(fn, *its)
        def shutdown(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    cf.ProcessPoolExecutor = SyncExecutor

    opened = set()
    fixdir = os.path.join(ROOT, "tests", "fixtures")
    real_open = builtins.open
    def tracking_open(file, *a, **k):
        try:
            p = os.path.abspath(os.fspath(file))
            if p.startswith(fixdir):
                opened.add(os.path.relpath(p, ROOT))
        except Exception:
            pass
        return real_open(file, *a, **k)
    builtins.open = tracking_open
    io.open = tracking_open
    import gzip
    real_gzip_open = gzip.open
    def tracking_gzip(file, *a, **k):
        try:
            p = os.path.abspath(os.fspath(file))
            if p.startswith(fixdir):
                opened.add(os.path.relpath(p, ROOT))
        except Exception:
            pass
        return real_gzip_open(file, *a, **k)
    gzip.open = tracking_gzip

    mon = sys.monitoring
    TOOL = 4
    mon.use_tool_id(TOOL, "iso")
    hits = set()
    prefix = os.path.join(ROOT, "sqlglot") + os.sep
    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            hits.add((fn, line, bool(code.co_flags & 0x02)))
        return mon.DISABLE
    used = set()
    def _key(obj):
        m = getattr(obj, "__module__", None)
        if isinstance(m, str) and m.startswith("sqlglot"):
            return m + ":" + getattr(obj, "__qualname__", "?")
        return None
    phase = ["import"]
    def on_call(code, offset, callable_, arg0):
        if phase[0] == "import":
            try:
                if isinstance(callable_, type):
                    k = _key(callable_)
                    if k: used.add(k)
            except Exception:
                pass
            return
        try:
            if isinstance(callable_, type):
                k = _key(callable_)
                if k: used.add(k)
            else:
                owner = getattr(callable_, "__self__", None)
                if isinstance(owner, type):
                    k = _key(owner)
                    if k: used.add(k)
                if isinstance(arg0, type):
                    k = _key(arg0)
                    if k: used.add(k)
        except Exception:
            pass
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.CALL, on_call)
    mon.set_events(TOOL, mon.events.LINE | mon.events.CALL)
    ok = None
    err = ""
    try:
        if test["id"] == "__import_sqlglot__":
            importlib.import_module("sqlglot")
            raise SystemExit
        file, cls, meth = test["id"].split("::")
        mod = importlib.import_module(file[:-3].replace("/", "."))
        case = getattr(mod, cls)(meth)
        phase[0] = "run"
        suite = unittest.TestSuite([case])
        result = unittest.TestResult()
        suite.run(result)
        suite._tearDownPreviousClass(None, result)
        suite._handleModuleTearDown(result)
        ok = result.wasSuccessful()
        if not ok:
            err = repr((result.failures + result.errors)[:1])[:500]
    except BaseException as e:  # noqa
        ok = False
        err = repr(e)[:500]
    finally:
        mon.set_events(TOOL, 0)
    run, dfn = {}, {}
    for fn, line, is_func in hits:
        rel = os.path.relpath(fn, ROOT)
        (run if is_func else dfn).setdefault(rel, []).append(line)
    lines = {"run": {k: sorted(v) for k, v in run.items()}, "def": {k: sorted(v) for k, v in dfn.items()}}
    with real_open(os.path.join(OUT, f"{test['number']}.json"), "w") as fh:
        json.dump({"ok": ok, "err": err, "lines": lines["run"], "def": lines["def"], "classes": sorted(used), "fixtures": sorted(opened)}, fh)


running = {}
tests = tests + [{"id": "__import_sqlglot__", "number": "import", "seconds": 0}]
hi = hi + 1 if hi == len(tests) - 1 else hi
todo = [t for t in tests[lo:hi] if not os.path.exists(os.path.join(OUT, f"{t['number']}.json"))]
todo.sort(key=lambda t: -t["seconds"])  # long ones first
start = time.time()
done = 0
while todo or running:
    while todo and len(running) < JOBS:
        t = todo.pop(0)
        pid = os.fork()
        if pid == 0:
            try:
                child(t)
            finally:
                os._exit(0)
        running[pid] = t
    pid, _ = os.wait()
    running.pop(pid, None)
    done += 1
    if done % 100 == 0:
        print(f"{done} done, {time.time() - start:.0f}s", file=sys.stderr, flush=True)
print(f"all done in {time.time() - start:.0f}s", file=sys.stderr)
