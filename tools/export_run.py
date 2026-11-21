"""usage: export_run.py tests.json trace_dir out_dir -> run.json.gz, def.json.gz, fixture_files.json"""
import json, glob, gzip, os, sys
T = {t["id"]: t["number"] for t in json.load(open(sys.argv[1]))["tests"]}
LAZY = T["tests/dialects/test_dialect.py::TestDialect::test_lazy_load"]
run, dfn, fx = {}, {}, {}
for p in glob.glob(sys.argv[2] + "/*.json"):
    stem = os.path.basename(p)[:-5]; d = json.load(open(p))
    n = LAZY if stem == "import" else int(stem)
    for M, src in ((run, d["lines"]), (dfn, d["def"])):
        for f, ls in src.items():
            fi = M.setdefault(f, {})
            for l in ls: fi.setdefault(str(l), set()).add(n)
    for f in d["fixtures"]: fx.setdefault(f, set()).add(n)
OUTDIR = sys.argv[3]; os.makedirs(OUTDIR, exist_ok=True)
for name, M in (("run", run), ("def", dfn)):
    out = {f: {l: sorted(v) for l, v in sorted(d.items(), key=lambda x: int(x[0]))} for f, d in sorted(M.items())}
    with gzip.open(f"{OUTDIR}/{name}.json.gz", "wt", compresslevel=9) as h: json.dump(out, h, separators=(",", ":"))
    print(name, os.path.getsize(f"{OUTDIR}/{name}.json.gz"))
json.dump({k: sorted(v) for k, v in fx.items()}, open(OUTDIR + "/fixture_files.json", "w"))
