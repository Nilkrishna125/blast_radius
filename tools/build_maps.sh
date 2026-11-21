#!/bin/sh
# Rebuilds map/run.json.gz, map/classes.json, map/fixture_readers.json, map/attrs.json.gz and
# map/eqprofile.json from the pinned
# sqlglot commit. Run once on a dev machine (takes ~3 min on 10 cores); the outputs are committed.
#   docker build -t br-env - < tools/env.Dockerfile   (python:3.13-slim + requirements.lock)
#   tools/build_maps.sh /path/to/sqlglot-at-eb11aec
# Pin --cpuset-cpus: test_execute_tpcds starts one process per visible CPU.
set -e
SRC=$(realpath "$1"); HERE=$(cd "$(dirname "$0")/.." && pwd); WORK=$(mktemp -d)
cp "$HERE/sample/tests.json" "$WORK/tests.json"; cp "$HERE"/tools/*.py "$WORK/"
mkdir -p "$WORK/trace"
docker run --rm --memory 4g --cpuset-cpus 0-9 -v "$SRC":/work:ro -v "$WORK":/iso -w /work br-env \
  python /iso/trace_tests.py /iso/tests.json /iso/trace 10
docker run --rm --memory 2g -v "$SRC":/work:ro -v "$WORK":/iso -w /work br-env python /iso/class_mro.py /iso/mro.json
python3 "$WORK/export_run.py" "$WORK/tests.json" "$WORK/trace" "$WORK/out"
python3 "$WORK/export_classes.py" "$WORK/tests.json" "$WORK/trace" "$WORK/mro.json" "$SRC" "$HERE/map/classes.json"
cp "$WORK/out/run.json.gz" "$HERE/map/"
# class-attribute reads per test
mkdir -p "$WORK/attrs"
docker run --rm --memory 5g --cpuset-cpus 0-9 -v "$SRC":/work:ro -v "$WORK":/iso -w /work br-env \
  python /iso/trace_attrs.py /iso/tests.json /iso/attrs 10
python3 "$WORK/export_attrs.py" "$WORK/tests.json" "$WORK/attrs" "$HERE/map/attrs.json.gz" "$WORK/mro.json" "$SRC"
# equality-semantics profile of `is` / `is not` sites (one full pytest run)
mkdir -p "$WORK/eq"
docker run --rm --memory 8g --cpuset-cpus 0-9 -v "$SRC":/work:ro -v "$HERE/tools/eqprofile":/eqp:ro -v "$WORK":/eq -w /work \
  -e SKIP_INTEGRATION=0 -e PYTHONPATH=/eqp:/work -e EQ_ROOT=/work -e EQ_OUT=/eq/eq br-env \
  python -m pytest -q -n 8 -p eqplug -p no:cacheprovider tests
python3 "$HERE/tools/export_eq.py" "$WORK/eq" "$HERE/map/eqprofile.json"
# per-query TPC map (map/tpc.json.gz); map/state.json is built inside the image (Dockerfile)
docker run --rm --memory 6g --cpuset-cpus 0-9 -v "$SRC":/work:ro -v "$WORK":/iso -w /work br-env python -W ignore /iso/trace_tpcds.py /iso/tpc.json
python3 - "$WORK/tpc.json" "$HERE/map/tpc.json.gz" <<'PY'
import gzip, json, sys
d = json.load(open(sys.argv[1])); ex = {}
for q in d["tpcds"]:
    for f, ls in q["exec"].items():
        ex.setdefault(f, set()).update(ls)
out = {"setup": d["setup"], "exec": {f: sorted(v) for f, v in ex.items()},
       "tpcds": [{k: q.get(k) for k in ("i", "execute", "sql", "fp", "duck_sql", "front")} for q in d["tpcds"]],
       "tpch": d["tpch"]}
with gzip.open(sys.argv[2], "wt", compresslevel=9) as h:
    json.dump(out, h, separators=(",", ":"))
PY
python3 - "$WORK/out/fixture_files.json" "$HERE/map/fixture_readers.json" <<'PY'
import json, os, sys
r = {}
for p, ts in json.load(open(sys.argv[1])).items():
    r.setdefault(os.path.splitext(os.path.basename(p))[0], set()).update(ts)
json.dump({k: sorted(v) for k, v in sorted(r.items())}, open(sys.argv[2], "w"), separators=(",", ":"))
PY
