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
GEN_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"
ANNOTATE_FUNCS = "tests/test_optimizer.py::TestOptimizer::test_annotate_funcs"
MAX_SUBSET_DIALECTS = 8
CHILD_SECONDS = 40

_TPC = None
_BASE_DIR = _MAP_DIR = None


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

    real_plan = executor.Plan
    executor.Plan = capture
    by_i = {q["i"]: q for q in data["tpcds"]}
    differing = []
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
            differing.append(q)
            if len(differing) > MAX_TPCDS_EXEC:
                return False
    if not differing:
        return True
    executor.Plan = real_plan
    return _exec_tpcds(differing)


MAX_TPCDS_EXEC = 12


def _load_tpcds_table(name, columns):
    """Rows exactly as TestExecutor.setUpClass builds them from the CSV."""
    import ast, csv, gzip
    from sqlglot.executor.table import Table
    path = os.path.join(_BASE_DIR, "tests", "fixtures", "optimizer", "tpc-ds", f"{name}.csv.gz")
    with gzip.open(path, "rt", newline="") as handle:
        reader = csv.reader(handle, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def _exec_tpcds(queries):
    """Run these TPC-DS queries the way test_execute_tpcds does (pickled tree, Python executor,
    pickled result) and compare with the pinned DuckDB results."""
    import pickle
    import numpy as np
    import pandas as pd
    from pandas.testing import assert_frame_equal
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA
    with open(os.path.join(_MAP_DIR, "tpcds_expected.pkl"), "rb") as fh:
        expected = pickle.load(fh)
    loaded = {}
    for q in queries:
        if q["i"] not in expected:
            return False
        tree = pickle.loads(pickle.dumps(parse_one(q["sql"])))
        tables = {}
        for t in find_tables(tree):
            if t.name not in loaded:
                loaded[t.name] = _load_tpcds_table(t.name, TPCDS_SCHEMA[t.name])
            tables[t.name] = loaded[t.name]
        table = pickle.loads(pickle.dumps(execute(tree, schema=TPCDS_SCHEMA, tables=tables)))
        if table is None:
            continue
        b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
        try:
            assert_frame_equal(expected[q["i"]], b, check_dtype=False, check_index_type=False)
        except AssertionError:
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
_GRAPH = None
_DIALECT_ITEMS = None


def affected_dialects(state_path, owners, paths):
    """Parent side: dialects whose classes own a changed key or live in a touched file."""
    base_state(state_path)
    per = _DIALECT_ITEMS or {}
    if not per:
        return None
    affected = set()
    for item in list(owners) + list(paths):
        hit = {d for d, items in per.items() if item in items}
        if not hit:
            return None
        affected |= hit
    if len(affected) == len(per):
        return None
    return sorted(affected)


def base_state(state_path):
    global _STATE, _GRAPH
    if _STATE is None:
        try:
            with open(state_path) as fh:
                data = json.load(fh)
            _STATE = (data["snapshot"], set(data["unstable"]))
            _GRAPH = data.get("graph")
            global _DIALECT_ITEMS
            _DIALECT_ITEMS = {d: set(items) for d, items in (data.get("dialects") or {}).items()}
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
            import importlib, state
            names = set()
            for path in patched:
                name = path[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                names.add(name)
            graph = _GRAPH
            affected = state.dependents(graph, names) if graph else None
            if affected is None or any(n in ("sqlglot", "sqlglot.dialects.dialect", "sqlglot.dialects") for n in names):
                state.load_all()
                affected = None
            else:
                import sqlglot  # noqa
                affected.discard("sqlglot.__main__")
                for name in sorted(affected):
                    try:
                        importlib.import_module(name)
                    except Exception:
                        if name in names:
                            raise
                        # same as state.load_all(): modules that do not import are skipped
                # dialects build classes when their module loads; nothing else is lazy
            for name in names:
                if name not in sys.modules:
                    raise ImportError(name)
            snap = state.snapshot(base, only=affected)
            if affected is None:
                keys = set(snap) | set(pinned)
            else:
                prefixes = tuple(m.replace(".", "/") for m in affected)
                def mine(k):
                    path = k.split("|", 1)[0].split(":", 1)[0]
                    stem = path[:-3]
                    if stem.endswith("/__init__"):
                        stem = stem[: -len("/__init__")]
                    return stem in prefixes
                keys = set(snap) | {k for k in pinned if mine(k)}
            changed = sorted(k for k in keys if snap.get(k) != pinned.get(k) and k not in unstable)
            payload = json.dumps({"changed": changed, "added": [k for k in changed if k not in pinned],
                                  "removed": [k for k in changed if k not in snap]}).encode()
        except BaseException as error:
            if os.environ.get("DEBUG_HEAVY") == "1":
                import traceback
                traceback.print_exc()
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


def _affected_dialects(base, owners, paths):
    """Dialect names whose own classes (dialect, tokenizer, parser, generator and every class
    in their MROs) own one of the changed keys `owners` or live in one of `paths`. None if
    some change is not specific to a dialect (then every dialect is affected)."""
    from sqlglot.dialects import Dialects
    from sqlglot.dialects.dialect import Dialect

    def rel(cls):
        mod = sys.modules.get(cls.__module__)
        f = getattr(mod, "__file__", None) or ""
        return os.path.relpath(f, base) if f else None

    per_dialect = {}
    for d in Dialects:
        try:
            klass = type(Dialect.get_or_raise(d.value))
        except Exception:
            return None
        keys, files = set(), set()
        for attr in (None, "tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
            top = klass if attr is None else getattr(klass, attr, None)
            if not isinstance(top, type):
                continue
            for c in top.__mro__:
                path = rel(c)
                if not path or not path.startswith("sqlglot/"):
                    continue
                keys.add(path + ":" + c.__qualname__)
                files.add(path)
            if attr is not None and top.__module__ == "sqlglot.dialects.dialect":
                keys.add(rel(klass) + ":" + klass.__qualname__ + ".<" + attr + ">")
        per_dialect[d.value] = (keys, files)
    affected = set()
    for owner in owners:
        hit = {name for name, (keys, files) in per_dialect.items() if owner in keys}
        if not hit:
            return None
        affected |= hit
    for path in paths:
        hit = {name for name, (keys, files) in per_dialect.items() if path in files}
        if not hit:
            return None
        affected |= hit
    if len(affected) == len(per_dialect):
        return None
    return sorted(affected)


def _check_gen_memory(dialects):
    """tests/test_generator.py::TestGenerator::test_generator_memory for these dialects."""
    import gc, tracemalloc
    from sqlglot.dialects.dialect import Dialect
    for name in dialects:
        dialect = Dialect.get_or_raise(name)
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if not after - before < 1024:
            return False
    return True


def _check_annotate_funcs(dialects):
    """tests/test_optimizer.py::TestOptimizer::test_annotate_funcs, pairs of these dialects."""
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.annotate_types import annotate_types
    from tests.helpers import load_sql_fixture_pairs
    wanted = set(dialects)
    test_schema = {
        "tbl": {
            "bin_col": "BINARY", "str_col": "STRING", "bignum_col": "BIGNUMERIC", "date_col": "DATE",
            "decfloat_col": "DECFLOAT", "float_col": "FLOAT", "timestamp_col": "TIMESTAMP",
            "double_col": "DOUBLE", "bigint_col": "BIGINT", "smallint_col": "SMALLINT", "bit_col": "BIT",
            "obj_col": "OBJECT", "int_col": "INT", "bool_col": "BOOLEAN", "bytes_col": "BYTES",
            "interval_col": "INTERVAL", "array_col": "ARRAY<STRING>",
        }
    }
    for meta, sql, expected in load_sql_fixture_pairs("optimizer/annotate_functions.sql"):
        sql = f"SELECT {sql} FROM tbl"
        for dialect in (meta.get("dialect") or "").split(", "):
            if dialect not in wanted:
                continue
            annotated = annotate_types(parse_one(sql, read=dialect), dialect=dialect, schema=test_schema)
            result = annotated.expressions[0]
            for expression in result.walk():
                for arg_key, arg in expression.args.items():
                    for child in arg if isinstance(arg, list) else [arg]:
                        if isinstance(child, exp.Expr):
                            if child.arg_key != arg_key or child.parent is not expression:
                                return False
            if result.type.sql(dialect) != exp.DataType.build(expected, dialect=dialect).sql(dialect):
                return False
    return True


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
            global _BASE_DIR, _MAP_DIR
            _BASE_DIR, _MAP_DIR = base, map_dir
            data = tpc_map(map_dir)
            import warnings
            warnings.simplefilter("ignore")
            order = [OPT_TPCH, TPCH, MAX_NODES, TPCDS, ANNOTATE_FUNCS, GEN_MEMORY]
            for test_id, arg in sorted(checks.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
                try:
                    if test_id == TPCDS:
                        ok = _check_tpcds(data, arg)
                    elif test_id == TPCH:
                        ok = _check_tpch(data, optimized=False)
                    elif test_id == OPT_TPCH:
                        ok = _check_tpch(data, optimized=True)
                    elif test_id == MAX_NODES:
                        ok = _check_max_nodes()
                    elif test_id == GEN_MEMORY:
                        ok = _check_gen_memory(arg)
                    elif test_id == ANNOTATE_FUNCS:
                        ok = _check_annotate_funcs(arg)
                    else:
                        ok = False
                except BaseException:
                    ok = False
                if ok:
                    os.write(write_fd, (test_id + "\n").encode())
        except BaseException:
            pass
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
    text = b"".join(chunks).decode(errors="replace")
    # only complete lines count (a check cut off by the alarm proves nothing)
    return {line for line in text.split("\n")[:-1] if line in checks}


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
