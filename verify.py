"""Decision-time checks for the heavy tests, run on the patched sqlglot in a forked child.

Each check answers "can this test still fail?" by recomputing, on the patched code, the
outputs the test depends on and comparing them with the pinned commit's recorded outputs
(map/tpc.json.gz). It never runs the slow part of the test itself.

* test_execute_tpcds: parse every affected query, and for executed ones compare the DuckDB
  transpile and the fingerprint of the optimized expression handed to the planner. Only used
  when the patch touches nothing that the executor's run phase (planner, Python executor,
  pickling) executes.
* test_execute_tpch / test_optimized_tpch: parse and transpile outputs of the 22 queries.
* test_max_nodes: the same assertions on a statement 1/50th the width, node limits scaled.

A check that is unsure, errors, or runs out of time answers "keep the test".
"""
import gzip, hashlib, json, os, signal, sys

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCH = "tests/test_executor.py::TestExecutor::test_execute_tpch"
OPT_TPCH = "tests/test_executor.py::TestExecutor::test_optimized_tpch"
MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
CHILD_SECONDS = 40

_TPC = None


def tpc_map(map_dir):
    global _TPC
    if _TPC is None:
        try:
            with gzip.open(os.path.join(map_dir, "tpc.json.gz"), "rt") as handle:
                data = json.load(handle)
            data["setup"] = {f: set(v) for f, v in data["setup"].items()}
            data["exec"] = {f: set(v) for f, v in data["exec"].items()}
            for q in data["tpcds"]:
                q["front"] = {f: set(v) for f, v in q["front"].items()}
            _TPC = data
        except Exception as error:
            print(f"tpc map unavailable: {error}", file=sys.stderr)
            _TPC = {}
    return _TPC


def plan_tpcds(data, touched):
    """touched: {path: set(old line numbers)}. Returns ("keep" | "drop" | "check", queries)."""
    if not data:
        return "keep", None
    exec_lines, setup = data["exec"], data["setup"]
    import_time = False
    for path, lines in touched.items():
        if lines & exec_lines.get(path, set()):
            return "keep", None
        if lines & setup.get(path, set()):
            if path in exec_lines:
                return "keep", None
            import_time = True
    if import_time:
        return "check", [q["i"] for q in data["tpcds"]]
    hit = [q["i"] for q in data["tpcds"] if any(lines & q["front"].get(path, set()) for path, lines in touched.items())]
    return ("check", hit) if hit else ("drop", [])


# ---------------------------------------------------------------------------- child side

def _install_overlay(base, patched):
    """Serve patched sources for the changed sqlglot modules, everything else from base."""
    import importlib.abc, importlib.util
    sources = {}
    for path, text in patched.items():
        name = path[:-3].replace("/", ".")
        is_pkg = name.endswith(".__init__")
        if is_pkg:
            name = name[: -len(".__init__")]
        sources[name] = (path, text, is_pkg)

    class Loader(importlib.abc.Loader):
        def __init__(self, path, text):
            self.path, self.text = path, text
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            code = compile(self.text, os.path.join(base, self.path), "exec", dont_inherit=True)
            exec(code, module.__dict__)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            entry = sources.get(name)
            if entry is None:
                return None
            rel, text, is_pkg = entry
            location = os.path.join(base, rel)
            spec = importlib.util.spec_from_loader(name, Loader(rel, text), origin=location, is_package=is_pkg)
            if is_pkg:
                spec.submodule_search_locations = [os.path.dirname(location)]
            spec.has_location = True
            return spec

    sys.meta_path.insert(0, Finder())
    sys.path.insert(0, base)


def _check_tpcds(data, queries):
    import sqlglot
    import sqlglot.executor as executor
    from sqlglot import exp, parse_one, serde, transpile
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA

    class Stop(Exception):
        pass

    captured = {}

    def capture(expression):
        captured["fp"] = hashlib.sha1(json.dumps(serde.dump(expression)).encode()).hexdigest()
        raise Stop

    executor.Plan = capture
    by_i = {q["i"]: q for q in data["tpcds"]}
    for i in queries:
        q = by_i[i]
        tree = parse_one(q["sql"])
        if not q["execute"]:
            continue
        if transpile(q["sql"], write="duckdb")[0] != q["duck_sql"]:
            return False
        tables = {}
        for t in tree.find_all(exp.Table):
            if t.name in TPCDS_SCHEMA:
                tables[t.name] = Table(columns=list(TPCDS_SCHEMA[t.name]))
        captured.clear()
        try:
            executor.execute(tree, schema=TPCDS_SCHEMA, tables=tables)
        except Stop:
            pass
        if captured.get("fp") != q["fp"]:
            return False
    return True


def _check_tpch(data, optimized):
    from sqlglot import parse_one, transpile
    for q in data["tpch"]:
        parse_one(q["sql"])
        if optimized:
            if transpile(q["sql"], write="duckdb")[0] != q["duck_sql"]:
                return False
            if transpile(q["optimized"], write="duckdb")[0] != q["duck_optimized"]:
                return False
    return True


def _check_max_nodes():
    """tests/test_parser.py::TestParser::test_max_nodes with a 200-wide statement instead of
    10000-wide and both node limits scaled by the same 1/50."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError
    if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
        return False
    cols = [f"col{i}" for i in range(500)]
    if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select):
        return False
    width, depth, scale = 200, 100, 50
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join(expr for _ in range(width))}"
    for limit, needle in ((1_000_000 // scale, "Maximum number of AST nodes"), (50_000 // scale, f"({50_000 // scale})")):
        try:
            parse_one(sql, max_nodes=limit)
            return False
        except ParseError as error:
            if needle not in str(error):
                return False
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        return False
    except ParseError:
        pass
    return isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)


_STATE = None


def base_state(state_path):
    global _STATE
    if _STATE is None:
        try:
            with open(state_path) as fh:
                data = json.load(fh)
            _STATE = (data["snapshot"], set(data["unstable"]))
        except Exception as error:
            print(f"state snapshot unavailable: {error}", file=sys.stderr)
            _STATE = ({}, set())
    return _STATE


def state_diff(base, state_path, patched):
    """Import the patched sqlglot (every module and dialect) in a child and return the
    snapshot keys whose value differs from the pinned build, or None if that fails."""
    pinned, unstable = base_state(state_path)
    if not pinned:
        return None
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        payload = b"null"
        try:
            os.close(read_fd)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(CHILD_SECONDS)
            import warnings
            warnings.simplefilter("ignore")
            _install_overlay(base, patched)
            import state
            state.load_all()
            # every module must import on the patched code too
            for path in patched:
                name = path[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                if name not in sys.modules:
                    raise ImportError(name)
            snap = state.snapshot(base)
            changed = sorted(k for k in set(snap) | set(pinned) if snap.get(k) != pinned.get(k) and k not in unstable)
            payload = json.dumps({"changed": changed, "added": [k for k in changed if k not in pinned],
                                  "removed": [k for k in changed if k not in snap]}).encode()
        except BaseException:
            payload = b"null"
        try:
            view = memoryview(payload)
            while view:
                n = os.write(write_fd, view)
                view = view[n:]
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    try:
        return json.loads(b"".join(chunks).decode() or "null")
    except Exception:
        return None


def run_checks(base, map_dir, patched, checks):
    """Fork a child that imports the patched sqlglot and runs `checks` ({test_id: args}).
    Returns the set of test ids proven unable to fail."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        safe = []
        try:
            os.close(read_fd)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            signal.alarm(CHILD_SECONDS)
            os.environ["SKIP_INTEGRATION"] = "0"
            _install_overlay(base, patched)
            data = tpc_map(map_dir)
            import warnings
            warnings.simplefilter("ignore")
            for test_id, arg in checks.items():
                try:
                    if test_id == TPCDS:
                        ok = _check_tpcds(data, arg)
                    elif test_id == TPCH:
                        ok = _check_tpch(data, optimized=False)
                    elif test_id == OPT_TPCH:
                        ok = _check_tpch(data, optimized=True)
                    elif test_id == MAX_NODES:
                        ok = _check_max_nodes()
                    else:
                        ok = False
                except BaseException:
                    ok = False
                if ok:
                    safe.append(test_id)
        except BaseException:
            pass
        try:
            os.write(write_fd, json.dumps(safe).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    try:
        return set(json.loads(b"".join(chunks).decode() or "[]"))
    except Exception:
        return set()


def _run_tests_child(test_ids):
    """Run tests (unittest, fresh import of the patched code). Returns the failing ids."""
    import importlib, unittest
    failing = set()
    by_class = {}
    for test_id in test_ids:
        file, cls, meth = test_id.split("::")
        by_class.setdefault((file, cls), []).append(meth)
    for (file, cls), meths in sorted(by_class.items()):
        ids = [f"{file}::{cls}::{m}" for m in meths]
        try:
            mod = importlib.import_module(file[:-3].replace("/", "."))
            klass = getattr(mod, cls)
            cases = [klass(m) for m in meths]
        except BaseException:
            failing.update(ids)
            continue
        suite = unittest.TestSuite(cases)
        result = unittest.TestResult()
        try:
            suite.run(result)
            suite._tearDownPreviousClass(None, result)
            suite._handleModuleTearDown(result)
        except BaseException:
            failing.update(ids)
            continue
        for test, _ in result.failures + result.errors + [(t, None) for t in result.unexpectedSuccesses]:
            case = getattr(test, "test_case", test)  # subtest -> its test
            name = getattr(case, "_testMethodName", None)
            if name in meths and type(case).__name__ == cls:
                failing.add(f"{file}::{cls}::{name}")
            else:
                # class/module fixture errors (setUpClass, tearDownClass ...) fail every test
                failing.update(ids)
    return failing


def run_tests(base, patched, test_ids, seconds):
    """Fork a child that runs `test_ids` on the patched code. Returns the set of failing ids,
    or None when the run did not complete (caller then keeps every test)."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        payload = b"null"
        try:
            os.close(read_fd)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            signal.alarm(max(5, int(seconds)))
            os.environ["SKIP_INTEGRATION"] = "0"
            import warnings, logging
            warnings.simplefilter("ignore")
            logging.disable(logging.CRITICAL)
            _install_overlay(base, patched)
            payload = json.dumps(sorted(_run_tests_child(test_ids))).encode()
        except BaseException:
            payload = b"null"
        try:
            view = memoryview(payload)
            while view:
                n = os.write(write_fd, view)
                view = view[n:]
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    try:
        value = json.loads(b"".join(chunks).decode() or "null")
    except Exception:
        return None
    return None if value is None else set(value)
