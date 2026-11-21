"""Per-test class-attribute read tracing for sqlglot at the pinned commit.

Each test runs in its own forked child. In the child, every sqlglot module is instrumented
right after it finishes executing: data attributes in class bodies become logging
descriptors, and dict attributes become dicts that log the keys looked up in them. All of
sqlglot (and every dialect) is imported first, then the test module, then the test runs.

Logged reads:  "<path>:<qualname>|<attr>"           attribute read (any key / whole value)
               "<path>:<qualname>|<attr>|<key>"     one key looked up in a dict attribute
Reads made while sqlglot itself is importing are written by the "import" pseudo-run; they
mark attributes whose values feed import-time work (subclass bodies, templates, registries).
The "copies" pseudo-run lists dict entries shared by identity between classes ({**Base.X}).

usage (inside the labeling image, cwd = sqlglot checkout):
  python trace_attrs.py tests.json out_dir [jobs]
"""
import json, os, sys, time

TESTS_JSON, OUT = sys.argv[1], sys.argv[2]
JOBS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
tests = json.load(open(TESTS_JSON))["tests"]
os.makedirs(OUT, exist_ok=True)
ROOT = os.getcwd()
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, ROOT)


def key_repr(k):
    if isinstance(k, type):
        return "c:" + k.__qualname__
    if isinstance(k, str):
        return "s:" + k
    import enum
    if isinstance(k, enum.Enum):
        return "e:" + type(k).__name__ + "." + k.name
    return None


def child(test, mode):
    import builtins, importlib, importlib.abc, importlib.machinery, unittest, enum, types
    import concurrent.futures as cf

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

    reads = {"import": set(), "test": set()}
    phase = ["import"]
    log = reads["import"].add

    def setlog(name):
        nonlocal log
        phase[0] = name
        log = reads[name].add

    class Tap:
        __slots__ = ("k", "v")
        def __init__(self, k, v):
            self.k = k; self.v = v
        def __get__(self, obj, owner=None):
            if type(self.v) is not TapDict:
                log(self.k)
            return self.v

    class TapDict(dict):
        __slots__ = ("_k",)
        def __getitem__(self, key):
            r = key_repr(key)
            log(self._k + "|" + r if r else self._k + "|*v")
            return dict.__getitem__(self, key)
        def get(self, key, default=None):
            r = key_repr(key)
            log(self._k + "|" + r if r else self._k + "|*v")
            return dict.get(self, key, default)
        def __contains__(self, key):
            r = key_repr(key)
            log(self._k + "|" + r if r else self._k + "|*k")
            return dict.__contains__(self, key)
        def __iter__(self):
            log(self._k + "|*k"); return dict.__iter__(self)
        def keys(self):
            log(self._k + "|*k"); return dict.keys(self)
        def __len__(self):
            log(self._k + "|*k"); return dict.__len__(self)
        def values(self):
            log(self._k + "|*v"); return dict.values(self)
        def items(self):
            log(self._k + "|*v"); return dict.items(self)
        def copy(self):
            log(self._k + "|*v"); return dict(dict.items(self))
        def __or__(self, other):
            log(self._k + "|*v"); return dict(dict.items(self)) | other
        def __ror__(self, other):
            log(self._k + "|*v"); return other | dict(dict.items(self))
        def __eq__(self, other):
            log(self._k + "|*v"); return dict.__eq__(self, other)
        def __ne__(self, other):
            log(self._k + "|*v"); return dict.__ne__(self, other)
        def __repr__(self):
            log(self._k + "|*v"); return dict.__repr__(self)
        def pop(self, key, *default):
            r = key_repr(key)
            log(self._k + "|" + r if r else self._k + "|*v")
            return dict.pop(self, key, *default)
        def setdefault(self, key, default=None):
            r = key_repr(key)
            log(self._k + "|" + r if r else self._k + "|*v")
            return dict.setdefault(self, key, default)
        __hash__ = None
        def __reduce__(self):
            return (dict, (dict(dict.items(self)),))

    def relpath(mod):
        f = getattr(mod, "__file__", None) or ""
        return os.path.relpath(f, ROOT) if f.startswith(ROOT) else None

    done_classes = set()

    def instrument_class(cls, path, prefix):
        if id(cls) in done_classes:
            return
        done_classes.add(id(cls))
        if issubclass(cls, enum.Enum):
            return
        qual = prefix + cls.__name__
        for name, value in list(vars(cls).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(value, type):
                if value.__module__ == cls.__module__ and value.__qualname__.startswith(cls.__qualname__ + "."):
                    instrument_class(value, path, qual + ".")
                continue
            if callable(value) or hasattr(type(value), "__get__") or isinstance(value, (types.ModuleType,)):
                continue
            k = path + ":" + qual + "|" + name
            if type(value) is dict:
                d = TapDict(value); d._k = k
                value = d
            try:
                setattr(cls, name, Tap(k, value))
            except Exception:
                pass

    def instrument_module(mod):
        path = relpath(mod)
        if not path:
            return
        for v in list(vars(mod).values()):
            if isinstance(v, type) and v.__module__ == mod.__name__ and "." not in v.__qualname__:
                instrument_class(v, path, "")

    class Loader(importlib.abc.Loader):
        def __init__(self, inner):
            self.inner = inner
        def create_module(self, spec):
            return self.inner.create_module(spec)
        def exec_module(self, module):
            self.inner.exec_module(module)
            try:
                instrument_module(module)
            except Exception as e:
                print("instrument failed", module.__name__, e, file=sys.stderr)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if not name.startswith("sqlglot"):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec and spec.loader and spec.origin and spec.origin.startswith(ROOT):
                spec.loader = Loader(spec.loader)
            return spec

    sys.meta_path.insert(0, Finder())

    ok = None; err = ""
    try:
        import pkgutil
        import sqlglot
        for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
            if info.name == "sqlglot.__main__":
                continue
            try:
                importlib.import_module(info.name)
            except Exception:
                pass
        from sqlglot.dialects import Dialects
        from sqlglot.dialects.dialect import Dialect
        for d in Dialects:
            try:
                Dialect.get_or_raise(d.value)
            except Exception:
                pass
        if mode == "copies":
            out = {}
            seen = {}
            for name, mod in list(sys.modules.items()):
                path = relpath(mod) if name.startswith("sqlglot") else None
                if not path:
                    continue
                stack = [(v, "") for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name and "." not in v.__qualname__]
                while stack:
                    cls, prefix = stack.pop()
                    qual = prefix + cls.__name__
                    for an, tap in list(vars(cls).items()):
                        if isinstance(tap, type) and tap.__qualname__.startswith(cls.__qualname__ + "."):
                            stack.append((tap, qual + "."))
                        if isinstance(tap, Tap) and isinstance(tap.v, dict):
                            for kk, vv in dict.items(tap.v):
                                r = key_repr(kk)
                                if r is None or isinstance(vv, (str, int, float, bool, type(None), enum.Enum)):
                                    continue
                                seen.setdefault((an, r, id(vv)), set()).add(path + ":" + qual)
            for (an, r, _), owners in seen.items():
                if len(owners) > 1:
                    for o in owners:
                        out.setdefault(o + "|" + an + "|" + r, set()).update(owners - {o})
            with builtins.open(os.path.join(OUT, "copies.json"), "w") as fh:
                json.dump({k: sorted(v) for k, v in out.items()}, fh)
            os._exit(0)
        if mode == "import":
            with builtins.open(os.path.join(OUT, "import.json"), "w") as fh:
                json.dump(sorted(reads["import"]), fh)
            os._exit(0)
        setlog("test")
        file, cls, meth = test["id"].split("::")
        mod = importlib.import_module(file[:-3].replace("/", "."))
        case = getattr(mod, cls)(meth)
        suite = unittest.TestSuite([case])
        result = unittest.TestResult()
        suite.run(result)
        suite._tearDownPreviousClass(None, result)
        suite._handleModuleTearDown(result)
        ok = result.wasSuccessful()
        if not ok:
            err = repr((result.failures + result.errors)[:1])[:800]
    except BaseException as e:  # noqa
        ok = False
        err = repr(e)[:800]
    with builtins.open(os.path.join(OUT, f"{test['number']}.json"), "w") as fh:
        json.dump({"ok": ok, "err": err, "reads": sorted(reads["test"])}, fh)


def run(jobs_list):
    running = {}
    start = time.time(); done = 0
    while jobs_list or running:
        while jobs_list and len(running) < JOBS:
            t, mode = jobs_list.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t, mode)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, _ = os.wait()
        running.pop(pid, None); done += 1
        if done % 100 == 0:
            print(f"{done} done, {time.time() - start:.0f}s", file=sys.stderr, flush=True)
    print(f"all done in {time.time() - start:.0f}s", file=sys.stderr)


todo = [({"id": "-", "number": "-"}, "import"), ({"id": "-", "number": "-"}, "copies")]
todo += [(t, "test") for t in sorted(tests, key=lambda t: -t["seconds"]) if not os.path.exists(os.path.join(OUT, f"{t['number']}.json"))]
run(todo)
