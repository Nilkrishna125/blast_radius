"""usage: export_classes.py tests.json trace_dir mro.json sqlglot_checkout out.json"""
import json, glob, os, sys
mro = json.load(open(sys.argv[3]))
T = {t["id"]: t["number"] for t in json.load(open(sys.argv[1]))["tests"]}
LAZY = T["tests/dialects/test_dialect.py::TestDialect::test_lazy_load"]
used = {}
for p in glob.glob(sys.argv[2] + "/*.json"):
    stem = os.path.basename(p)[:-5]; d = json.load(open(p))
    n = LAZY if stem == "import" else int(stem)
    for k in d["classes"]: used.setdefault(k, set()).add(n)
cls = {}
for k, tests in used.items():
    for base in mro.get(k, [k]):
        cls.setdefault(base, set()).update(tests)
# key the map by "file path:qualname"
out = {}
for k, v in cls.items():
    mod, q = k.split(":", 1)
    path = mod.replace(".", "/")
    path = path + ("/__init__.py" if os.path.isdir(os.path.join(sys.argv[4], path)) else ".py")
    out[path + ":" + q] = sorted(v)
json.dump(out, open(sys.argv[5], "w"), separators=(",", ":"))

