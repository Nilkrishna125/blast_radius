"""Import-state snapshot of sqlglot: a stable hash of every module global and class attribute
after importing all of sqlglot and every dialect.

Keys:  "<path>|<name>"                   module global (data only)
       "<path>:<qualname>|<attr>"        class attribute (data, defaults of methods)
       "<path>:<qualname>|<attr>|<key>"  one entry of a dict attribute
Classes a dialect builds at import (tokenizer_class, parser_class, generator_class,
jsonpath_tokenizer_class without a class statement) are walked as
"<path>:<Dialect qualname>.<attr>".

Function objects are fingerprinted by their data only (closure cells, defaults), never by
their code: code changes are runtime changes, which coverage handles. So a dict entry
`rename_func("FIRST")` changes when its argument does, not when rename_func's body does.
Comparing the patched snapshot with the pinned one gives exactly the import-time state a
patch changes, including attributes derived at class creation (tries, escapes, ...).
"""
import enum, hashlib, importlib, os, pkgutil, sys, types

MAX_DEPTH = 64
DIALECT_CLASS_ATTRS = ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class")


def _key(k):
    if isinstance(k, type):
        return "c:" + k.__qualname__
    if isinstance(k, str):
        return "s:" + k
    if isinstance(k, enum.Enum):
        return "e:" + type(k).__name__ + "." + k.name
    return None


_MEMO = {}


def fp(v, depth=0, seen=None):
    """Deterministic text fingerprint of a value (no ids, no addresses). Results for container
    and function objects are memoized for the duration of one snapshot()."""
    if depth > MAX_DEPTH:
        return "~"
    if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
        return type(v).__name__ + ":" + repr(v)
    if seen is not None and id(v) in seen:
        return "^"
    memo = _MEMO.get(id(v))
    if memo is not None and memo[0] is v:
        return memo[1]
    result = _fp(v, depth, set() if seen is None else seen)
    # results that hit a cycle marker or the depth limit depend on the path: not reusable
    if "^" not in result and "~" not in result:
        _MEMO[id(v)] = (v, result)
    return result


def _fp(v, depth, seen):
    if isinstance(v, enum.Enum):
        return "E:" + type(v).__qualname__ + "." + str(v.name)
    if isinstance(v, type):
        return "T:" + getattr(v, "__module__", "?") + ":" + v.__qualname__
    if isinstance(v, types.ModuleType):
        return "M:" + v.__name__
    if id(v) in seen:
        return "^"
    seen = seen | {id(v)}
    if isinstance(v, (types.FunctionType, types.LambdaType)):
        cells = []
        for c in v.__closure__ or ():
            try:
                cells.append(fp(c.cell_contents, depth + 1, seen))
            except ValueError:
                cells.append("<empty>")
        return ("F:" + v.__qualname__ + "(" + ",".join(cells) + ")d" + fp(v.__defaults__, depth + 1, seen)
                + "k" + fp(v.__kwdefaults__, depth + 1, seen))
    if isinstance(v, (types.BuiltinFunctionType, types.MethodType, types.BuiltinMethodType)):
        owner = getattr(v, "__self__", None)
        return "B:" + getattr(v, "__qualname__", repr(type(v))) + ("@" + fp(owner, depth + 1, seen) if isinstance(owner, type) else "")
    if isinstance(v, (classmethod, staticmethod)):
        return type(v).__name__ + ":" + fp(v.__func__, depth + 1, seen)
    if isinstance(v, property):
        return "P:" + ",".join(fp(f, depth + 1, seen) for f in (v.fget, v.fset, v.fdel))
    try:
        import functools
        if isinstance(v, functools.partial):
            return "p:" + fp(v.func, depth + 1, seen) + fp(v.args, depth + 1, seen) + fp(v.keywords, depth + 1, seen)
    except Exception:
        pass
    if isinstance(v, dict):
        items = sorted((fp(k, depth + 1, seen), fp(x, depth + 1, seen)) for k, x in dict.items(v))
        return "{" + ",".join(a + ":" + b for a, b in items) + "}"
    if isinstance(v, (list, tuple)):
        parts = [fp(x, depth + 1, seen) for x in v]
        if v and all(isinstance(x, type) for x in v):
            parts.sort()  # tuples of classes (isinstance targets) are often built from sets
        return ("[" if isinstance(v, list) else "(") + ",".join(parts) + "]"
    if isinstance(v, (set, frozenset)):
        return "<" + ",".join(sorted(fp(x, depth + 1, seen) for x in v)) + ">"
    cls = type(v)
    mod = getattr(cls, "__module__", "") or ""
    if mod.startswith("sqlglot.expressions") or hasattr(v, "args") and hasattr(v, "arg_types"):
        try:
            return "X:" + repr(v)
        except Exception:
            pass
    state = getattr(v, "__dict__", None)
    if isinstance(state, dict):
        return "O:" + cls.__qualname__ + fp(state, depth + 1, seen)
    slots = getattr(cls, "__slots__", None)
    if slots:
        return "S:" + cls.__qualname__ + fp({s: getattr(v, s, None) for s in slots if isinstance(s, str)}, depth + 1, seen)
    return "o:" + cls.__qualname__


def _h(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _is_code_object(v):
    return isinstance(v, (types.FunctionType, classmethod, staticmethod, property))


def _kind(v):
    return type(v).__name__


def _walk_class(cls, owner, out, seen_classes, root):
    if id(cls) in seen_classes:
        return
    seen_classes.add(id(cls))
    methods = sorted(f"{n}:{_kind(v)}" for n, v in vars(cls).items() if callable(v) or isinstance(v, (classmethod, staticmethod, property)))
    out[owner + "|__methods__"] = _h(",".join(methods) + "|bases:" + ",".join(b.__module__ + ":" + b.__qualname__ for b in cls.__bases__))
    for name, v in list(vars(cls).items()):
        if name in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__slots__", "__annotations__", "__firstlineno__", "__static_attributes__", "_abc_impl"):
            continue
        if isinstance(v, type):
            if v.__qualname__.startswith(cls.__qualname__ + ".") and v.__module__ == cls.__module__:
                _walk_class(v, owner + "." + v.__name__, out, seen_classes, root)
            else:
                out[owner + "|" + name] = _h(fp(v))
            continue
        if isinstance(v, (types.FunctionType, classmethod, staticmethod)):
            func = v.__func__ if isinstance(v, (classmethod, staticmethod)) else v
            data = fp(func.__defaults__) + fp(func.__kwdefaults__)
            if data != "NoneType:NoneNoneType:None":
                out[owner + "|" + name + "|__defaults__"] = _h(data)
            continue
        if isinstance(v, property):
            continue
        out[owner + "|" + name] = _h(fp(v))
        if isinstance(v, dict):
            for k, x in dict.items(v):
                r = _key(k)
                if r is not None:
                    out[owner + "|" + name + "|" + r] = _h(fp(x))
    # classes a dialect creates in its metaclass
    for attr in DIALECT_CLASS_ATTRS:
        sub = cls.__dict__.get(attr)
        if isinstance(sub, type) and id(sub) not in seen_classes and not sub.__qualname__.startswith(cls.__qualname__ + "."):
            if sub.__module__ == cls.__module__ or sub.__module__ == "sqlglot.dialects.dialect":
                if sub.__qualname__ in ("Tokenizer", "JSONPathTokenizer", "Parser", "Generator") and sub.__module__ == "sqlglot.dialects.dialect":
                    _walk_class(sub, owner + ".<" + attr + ">", out, seen_classes, root)


def load_all(base=None):
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


def snapshot(root):
    """{key: hash} for the currently imported sqlglot (call load_all first)."""
    _MEMO.clear()
    out = {}
    seen_classes = set()
    root = os.path.abspath(root)
    for name in sorted(n for n in list(sys.modules) if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None) or ""
        if not f:
            continue
        path = os.path.relpath(os.path.abspath(f), root)
        names = sorted(f"{n}:{_kind(v)}" for n, v in vars(mod).items() if not n.startswith("__"))
        out[path + "|__names__"] = _h(",".join(names))
        for gname, v in sorted(vars(mod).items()):
            if gname.startswith("__") and gname.endswith("__"):
                continue
            if isinstance(v, types.ModuleType):
                continue
            if isinstance(v, type):
                if v.__module__ == name and "." not in v.__qualname__:
                    _walk_class(v, path + ":" + v.__qualname__, out, seen_classes, root)
                continue
            if isinstance(v, types.FunctionType):
                if v.__module__ == name:
                    data = fp(v.__defaults__) + fp(v.__kwdefaults__)
                    if data != "NoneType:NoneNoneType:None":
                        out[path + "|" + gname + "|__defaults__"] = _h(data)
                continue
            out[path + "|" + gname] = _h(fp(v))
            if isinstance(v, dict):
                for k, x in dict.items(v):
                    r = _key(k)
                    if r is not None:
                        out[path + "|" + gname + "|" + r] = _h(fp(x))
    return out


if __name__ == "__main__":
    # usage: python state.py <sqlglot checkout> <out.json>
    # Run twice at image build (different PYTHONHASHSEED); merge with --merge a b out.
    import json
    if sys.argv[1] == "--merge":
        a, b = json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
        unstable = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        with open(sys.argv[4], "w") as fh:
            json.dump({"snapshot": a, "unstable": unstable}, fh, separators=(",", ":"))
        print("keys", len(a), "unstable", len(unstable), file=sys.stderr)
        sys.exit(0)
    import warnings
    warnings.simplefilter("ignore")
    root = sys.argv[1]
    sys.path.insert(0, root)
    load_all()
    snap = snapshot(root)
    with open(sys.argv[2], "w") as fh:
        json.dump(snap, fh, separators=(",", ":"))
    print("keys", len(snap), file=sys.stderr)
