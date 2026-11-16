"""Собирает сайт: страница + один файл data.json из документов сборщика.

python -I collector/build_site.py <state_dir> <out_dir>
"""
import json, os, shutil, sys

state, out = sys.argv[1], sys.argv[2]
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(out, exist_ok=True)
hashes = json.load(open(os.path.join(state, "hashes.json")))
data = {"days": {}, "reports": {}, "meta": {}}
for path in hashes:  # только актуальные документы, без удалённых
    coll, doc_id = path.split("/", 1)
    with open(os.path.join(state, "docs", path.replace("/", "__") + ".json"), encoding="utf-8") as f:
        data.setdefault(coll, {})[doc_id] = json.load(f)
with open(os.path.join(out, "data.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
shutil.copy(os.path.join(root, "site", "index.html"), os.path.join(out, "index.html"))
open(os.path.join(out, ".nojekyll"), "w").close()
print(json.dumps({k: len(v) for k, v in data.items()}))
