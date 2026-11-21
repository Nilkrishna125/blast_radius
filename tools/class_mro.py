"""Import every sqlglot module and dump {class_key: [mro class_keys]} for sqlglot classes."""
import importlib, json, pkgutil, sys, os
sys.path.insert(0, os.getcwd())
import sqlglot
from sqlglot.dialects import Dialects
from sqlglot.dialects.dialect import Dialect
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__": continue
    try: importlib.import_module(info.name)
    except Exception as e: print("skip", info.name, e, file=sys.stderr)
for d in Dialects:
    try: Dialect.get_or_raise(d.value)
    except Exception: pass
seen = {}
def key(c): return c.__module__ + ":" + c.__qualname__
def walk(c):
    if not isinstance(c, type) or not c.__module__.startswith("sqlglot"): return
    k = key(c)
    if k in seen: return
    seen[k] = [key(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
    for v in list(vars(c).values()):
        if isinstance(v, type): walk(v)
    for s in type.__subclasses__(c): walk(s)
for name, mod in list(sys.modules.items()):
    if name.startswith("sqlglot"):
        for v in list(vars(mod).values()): walk(v)
json.dump(seen, open(sys.argv[1], "w"))
print(len(seen), file=sys.stderr)
