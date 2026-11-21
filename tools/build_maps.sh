#!/bin/sh
# Rebuilds map/run.json.gz, map/classes.json and map/fixture_readers.json from the pinned
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
python3 - "$WORK/out/fixture_files.json" "$HERE/map/fixture_readers.json" <<'PY'
import json, os, sys
r = {}
for p, ts in json.load(open(sys.argv[1])).items():
    r.setdefault(os.path.splitext(os.path.basename(p))[0], set()).update(ts)
json.dump({k: sorted(v) for k, v in sorted(r.items())}, open(sys.argv[2], "w"), separators=(",", ":"))
PY
