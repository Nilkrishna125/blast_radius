"""usage: export_attrs.py tests.json trace_dir out.json.gz mro.json sqlglot_checkout
-> {"sets": [[tests...], ...], "reads": {readkey: set_index}, "import": [readkeys read while sqlglot imports],
    "copies": {"<attr>|<key>": [[owner, ...], ...]}}  (owners: "<path>:<qualname>")"""
import json, glob, gzip, os, sys
tests = json.load(open(sys.argv[1]))["tests"]; TR = sys.argv[2]
reads = {}
for t in tests:
    d = json.load(open(f"{TR}/{t['number']}.json"))
    for r in d["reads"]:
        reads.setdefault(r, []).append(t["number"])
sets, index, out = [], {}, {}
for r, ns in reads.items():
    key = tuple(sorted(set(ns)))
    if key not in index:
        index[key] = len(sets); sets.append(list(key))
    out[r] = index[key]
groups = {}
for k, others in json.load(open(f"{TR}/copies.json")).items():
    owner, attr, kr = k.split("|", 2)
    g = tuple(sorted(set(others) | {owner}))
    groups.setdefault(attr + "|" + kr, set()).add(g)
copies = {k: sorted(map(list, v)) for k, v in groups.items()}
mro = json.load(open(sys.argv[4]))
def as_path(k):
    mod, q = k.split(":", 1)
    path = mod.replace(".", "/")
    path = path + ("/__init__.py" if os.path.isdir(os.path.join(sys.argv[5], path)) else ".py")
    return path + ":" + q
subclasses = {}
for k, bases in mro.items():
    for b in bases[1:]:
        subclasses.setdefault(as_path(b), set()).add(as_path(k))
data = {"subclasses": {k: sorted(v) for k, v in subclasses.items()}, "sets": sets, "reads": out, "import": json.load(open(f"{TR}/import.json")), "copies": copies}
with gzip.open(sys.argv[3], "wt", compresslevel=9) as h:
    json.dump(data, h, separators=(",", ":"))
print("read keys", len(out), "sets", len(sets), "copy groups", sum(len(v) for v in copies.values()), "bytes", os.path.getsize(sys.argv[3]))
