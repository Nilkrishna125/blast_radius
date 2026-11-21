"""Equality-semantics profile: for every `a is b` / `a is not b` in sqlglot, record whether
`a == b` (resp. `a != b`) ever gave a different truth value during the test suite.

Installed via PYTHONPATH (so spawned worker processes load it too). Each process writes
$EQ_OUT/<pid>.json at exit: {"path:line:col": [evaluations, disagreements]}.
"""
import ast, atexit, builtins, importlib.abc, importlib.machinery, json, os, sys

ROOT = os.environ.get("EQ_ROOT", os.getcwd())
OUT = os.environ.get("EQ_OUT")
STATS = {}


def _br_is(site, a, b, negate):
    same = a is b
    s = STATS.get(site)
    if s is not None and s[1]:
        s[0] += 1
        return (not same) if negate else same
    try:
        eq = bool(a != b) if negate else bool(a == b)
        bad = eq != ((not same) if negate else same)
    except Exception:
        bad = True
    s = STATS.get(site)
    if s is None:
        s = STATS[site] = [0, 0]
    s[0] += 1
    if bad:
        s[1] += 1
    return (not same) if negate else same


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            call = ast.Call(
                func=ast.Name(id="__br_is__", ctx=ast.Load()),
                args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(isinstance(node.ops[0], ast.IsNot))],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # never use cached bytecode: it would skip the rewrite
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


if OUT:
    builtins.__br_is__ = _br_is
    sys.meta_path.insert(0, _Finder())
    sys.dont_write_bytecode = True

    @atexit.register
    def _dump():
        if not STATS:
            return
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"{os.getpid()}.json"), "w") as fh:
            json.dump(STATS, fh)
