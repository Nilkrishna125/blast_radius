"""usage: export_eq.py profile_dir out.json -> {"path:line:col": [evaluations, disagreements]}"""
import glob, json, sys
merged = {}
for p in glob.glob(sys.argv[1] + "/*.json"):
    for site, (n, bad) in json.load(open(p)).items():
        m = merged.setdefault(site, [0, 0]); m[0] += n; m[1] += bad
json.dump(merged, open(sys.argv[2], "w"), separators=(",", ":"))
print("sites", len(merged), "disagreeing", sum(1 for v in merged.values() if v[1]))
