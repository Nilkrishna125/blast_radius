"""usage: export_cmp.py tests.json profile_dir out.json
-> {"path:line:col": [test numbers] | "*"}; "*" when the swap mattered outside any test
(import time, between tests or in a test's setup we cannot narrow) for every test."""
import glob, json, sys
tests = json.load(open(sys.argv[1]))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_class, by_file = {}, {}
for t in tests:
    by_class.setdefault(t["id"].rsplit("::", 1)[0], []).append(t["number"])
    by_file.setdefault(t["file"], []).append(t["number"])
merged = {}
for p in glob.glob(sys.argv[2] + "/*.json"):
    for site, buckets in json.load(open(p)).items():
        merged.setdefault(site, set()).update(buckets)
out = {}
for site, buckets in merged.items():
    numbers = set()
    star = False
    for b in buckets:
        if b.startswith("collect:"):
            numbers.update(by_file.get(b[len("collect:"):], []))
            if b[len("collect:"):] not in by_file:
                star = True
            continue
        nodeid, _, phase = b.partition("|")
        key = nodeid.split(" [")[0]
        if key in num:
            if phase == "setup":
                numbers.update(by_class.get(key.rsplit("::", 1)[0], [num[key]]))
            else:
                numbers.add(num[key])
        else:
            star = True
    out[site] = "*" if star else sorted(numbers)
json.dump(out, open(sys.argv[3], "w"), separators=(",", ":"))
print("sites", len(out), "star", sum(1 for v in out.values() if v == "*"))
