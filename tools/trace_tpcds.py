"""Per-query map for the TPC executor tests in tests/test_executor.py.

test_execute_tpcds parses every TPC-DS query, executes the `execute` ones with sqlglot's
Python executor (in a process pool, so trees and results are pickled) and compares each
result with DuckDB running the query transpiled by sqlglot. test_execute_tpch does the same
for TPC-H (no query executes there), and test_optimized_tpch compares DuckDB on the
transpiled query with DuckDB on the transpiled expected optimization.

The parent imports sqlglot and the test module and runs TestExecutor.setUpClass; the lines
it runs are "setup". Each query then runs in its own forked child, split in two phases:
  front: parse, transpile to DuckDB, and execute() up to the logical plan (optimizer)
  exec:  the plan, the Python executor and the pickle round trips
The fingerprint of a query is a hash of serde.dump() of the optimized expression handed to
the planner (tree, types, comments). If a patch leaves every front-end output (fingerprint,
transpiled SQL, parse success) unchanged and touches nothing the exec phase runs, the test
cannot change.

usage (labeling image, cwd = sqlglot checkout):  python trace_tpcds.py out.json
"""
import hashlib, json, os, pickle, sys

OUT = sys.argv[1]
ROOT = os.getcwd()
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, ROOT)
PREFIX = os.path.join(ROOT, "sqlglot") + os.sep

mon = sys.monitoring
FRONT, EXEC = 3, 4
mon.use_tool_id(FRONT, "front")
mon.use_tool_id(EXEC, "exec")
hits = {FRONT: set(), EXEC: set()}


SETUP_ONLY_FUNCTIONS = [False]


def make_cb(tool):
    bucket = hits[tool]
    def cb(code, line):
        if code.co_filename.startswith(PREFIX):
            if SETUP_ONLY_FUNCTIONS[0] and not (code.co_flags & 0x02):
                return  # module/class bodies: import-time state, covered by the state diff
            bucket.add((code.co_filename, line))
        return mon.DISABLE
    return cb


mon.register_callback(FRONT, mon.events.LINE, make_cb(FRONT))
mon.register_callback(EXEC, mon.events.LINE, make_cb(EXEC))


def phase(tool):
    mon.set_events(FRONT, mon.events.LINE if tool == FRONT else 0)
    mon.set_events(EXEC, mon.events.LINE if tool == EXEC else 0)


# "setup" = function code that TestExecutor.setUpClass runs (imports are not recorded)

import concurrent.futures as cf  # noqa: E402


class SyncExecutor:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*initargs)
    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:
            f.set_exception(e)
        return f
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


cf.ProcessPoolExecutor = SyncExecutor

import sqlglot  # noqa: E402
import sqlglot.executor as executor  # noqa: E402
from sqlglot import parse_one, serde, transpile  # noqa: E402
from tests import test_executor as te  # noqa: E402
from tests.helpers import TPCDS_SCHEMA  # noqa: E402

SETUP_ONLY_FUNCTIONS[0] = True
phase(FRONT)
te.TestExecutor.setUpClass()
phase(None)
SETUP_ONLY_FUNCTIONS[0] = False


def lines_of(bucket):
    by = {}
    for fn, line in bucket:
        by.setdefault(os.path.relpath(fn, ROOT), []).append(line)
    return {k: sorted(v) for k, v in by.items()}


result = {"setup": lines_of(hits[FRONT]), "tpcds": [], "tpch": []}
real_plan = executor.Plan


def run_query(meta, sql, out_path):
    hits[FRONT].clear(); hits[EXEC].clear()
    mon.restart_events()
    rec = {}
    captured = {}

    def plan_wrapper(expression):
        captured["fp"] = hashlib.sha1(json.dumps(serde.dump(expression)).encode()).hexdigest()
        phase(EXEC)
        return real_plan(expression)

    executor.Plan = plan_wrapper
    try:
        phase(FRONT)
        tree = parse_one(sql)
        if meta.get("execute"):
            rec["duck_sql"] = transpile(sql, write="duckdb")[0]
            phase(EXEC)
            tree2 = pickle.loads(pickle.dumps(tree))
            phase(FRONT)
            te.initializer(TPCDS_SCHEMA, te.TestExecutor.tpcds_tables)
            # te.mp_execute -> execute(): front half until Plan(), exec afterwards
            table = te.mp_execute(tree2, meta)
            phase(EXEC)
            pickle.loads(pickle.dumps(table))
            rec["fp"] = captured.get("fp")
    except BaseException as e:  # noqa
        rec["error"] = repr(e)
    phase(None)
    rec["front"] = lines_of(hits[FRONT])
    rec["exec"] = lines_of(hits[EXEC])
    with open(out_path, "w") as fh:
        json.dump(rec, fh)


tmp = OUT + ".parts"
os.makedirs(tmp, exist_ok=True)
for i, (meta, sql, _) in enumerate(te.TestExecutor.tpcds_sqls):
    pid = os.fork()
    if pid == 0:
        run_query(meta, sql, f"{tmp}/{i}.json")
        os._exit(0)
    os.waitpid(pid, 0)
    rec = json.load(open(f"{tmp}/{i}.json"))
    rec.update({"i": i, "sql": sql, "execute": bool(meta.get("execute"))})
    result["tpcds"].append(rec)

# TPC-H: no query executes in test_execute_tpch; test_optimized_tpch transpiles both sides.
for i, (meta, sql, optimized) in enumerate(te.TestExecutor.tpch_sqls):
    result["tpch"].append({
        "i": i, "sql": sql, "optimized": optimized, "execute": bool(meta.get("execute")),
        "duck_sql": transpile(sql, write="duckdb")[0],
        "duck_optimized": transpile(optimized, write="duckdb")[0],
    })

# What cached_execute gives the test for each executed query: DuckDB on the pinned transpile.
expected = {}
for q in result["tpcds"]:
    if q["execute"] and q.get("duck_sql"):
        expected[q["i"]] = te.TestExecutor.tpcds_conn.execute(q["duck_sql"]).fetchdf()
with open(OUT + ".expected.pkl", "wb") as fh:
    pickle.dump(expected, fh, protocol=5)
with open(OUT, "w") as fh:
    json.dump(result, fh)
print("tpcds", len(result["tpcds"]), "fingerprints", sum(1 for q in result["tpcds"] if q.get("fp")),
      "errors", [q["i"] for q in result["tpcds"] if q.get("error")], file=sys.stderr)
