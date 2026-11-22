"""Comparison-swap profile. For every single `<`, `<=`, `>`, `>=`, `is`, `is not` in sqlglot,
record in which test the swapped operator would ever have given a different answer:
  <  vs <=, >  vs >= : the operands were equal
  is vs ==, is not vs != : identity and equality disagreed
The current test comes from the pytest plugin (cmpplug); evaluations outside any test are
recorded as "import". Each process writes $CMP_OUT/<pid>.json: {site: [buckets]}."""
import ast, builtins, importlib.abc, importlib.machinery, json, os, sys

ROOT = os.environ.get("CMP_ROOT", os.getcwd())
OUT = os.environ.get("CMP_OUT")
STATS = {}
CURRENT = ["import"]


def _br_cmp(site, a, b, op):
    if op == 0:
        result = a < b
    elif op == 1:
        result = a <= b
    elif op == 2:
        result = a > b
    elif op == 3:
        result = a >= b
    elif op == 4:
        result = a is b
    else:
        result = a is not b
    bucket = CURRENT[0]
    seen = STATS.get(site)
    if seen is not None and bucket in seen:
        return result
    try:
        if op < 4:
            differs = bool(a == b)
        elif op == 4:
            differs = bool(a == b) != result
        else:
            differs = bool(a != b) != result
    except Exception:
        differs = True
    if differs:
        STATS.setdefault(site, set()).add(bucket)
    return result


OPS = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Is: 4, ast.IsNot: 5}


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in OPS:
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            call = ast.Call(func=ast.Name(id="__br_cmp__", ctx=ast.Load()),
                            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
                            keywords=[])
            return ast.copy_location(call, node)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)

    def source_to_code(self, data, path, *, _optimize=-1):
        tree = _Rewrite(os.path.relpath(path, ROOT)).visit(ast.parse(data, filename=path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not name.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py") and spec.origin.startswith(ROOT):
            spec.loader = _Loader(name, spec.origin)
        return spec


def _dump():
    if not OUT or not STATS:
        return
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{os.getpid()}.json"), "w") as fh:
        json.dump({k: sorted(v) for k, v in STATS.items()}, fh)


def _run_in_bucket(bucket, fn, *args, **kwargs):
    """Runs a process-pool task under the bucket of the test that submitted it."""
    previous = CURRENT[0]
    CURRENT[0] = bucket
    try:
        return fn(*args, **kwargs)
    finally:
        CURRENT[0] = previous


def _patch_pools():
    import concurrent.futures.process as cfp
    original = cfp.ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        return original(self, _run_in_bucket, CURRENT[0], fn, *args, **kwargs)

    cfp.ProcessPoolExecutor.submit = submit
    original_init = cfp.ProcessPoolExecutor.__init__

    def init(self, *a, initializer=None, initargs=(), **k):
        # pool initializers run before any task: attribute them to the creating test
        if initializer is not None:
            original_init(self, *a, initializer=_run_in_bucket, initargs=(CURRENT[0], initializer, *initargs), **k)
        else:
            original_init(self, *a, **k)

    cfp.ProcessPoolExecutor.__init__ = init


if OUT:
    import atexit
    _patch_pools()
    builtins.__br_cmp__ = _br_cmp
    sys.meta_path.insert(0, _Finder())
    sys.dont_write_bytecode = True
    atexit.register(_dump)
