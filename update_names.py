"""Regenerate names.txt (mod ID -> character/NPC name) from the EN datamine.

Run this when new characters or skins come out:  python update_names.py
The mod manager itself only reads names.txt and never goes online.
"""
import json
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/Hiro420/StellaSoraData/HEAD/EN"
out = Path(__file__).with_name("names.txt")


def L(n):
    kind, _, table = n[:-5].partition("_")
    path = f"bin/{table}" if kind == "bin" else f"language/en_US/{table.split('_', 2)[-1]}"
    with urllib.request.urlopen(f"{SOURCE}/{path}.json", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


char, skin, npcskin = L("bin_Character.json"), L("bin_CharacterSkin.json"), L("bin_NPCSkin.json")
tx = {}
for n in ("language_en_US_Character.json", "language_en_US_CharacterSkin.json"):
    tx.update(L(n))
clean = lambda s: " ".join(s.split())
rows = {}
charname = {}
for cid, c in char.items():
    n = tx.get(c["Name"])
    if n: charname[int(cid)] = clean(n); rows[cid] = clean(n)
for sid, s in skin.items():
    base = charname.get(s["CharId"])
    if not base: continue
    title = clean(tx.get(s["Name"], ""))
    if title.lower().startswith(base.lower() + ":"):
        title = title.split(":", 1)[1].strip()
    rows[sid] = base if title in ("", "Default", "???", base) else f"{base} ({title})"
npcname = {}
for tbl in ("NPCConfig", "StarTowerNPC", "BoardNPC"):
    b, t = L(f"bin_{tbl}.json"), L(f"language_en_US_{tbl}.json")
    for k, c in b.items():
        n = t.get(c.get("Name", ""))
        if n and not n.startswith("Enhancement"):
            npcname.setdefault(int(k), clean(n))
            if int(k) >= 100000:
                rows.setdefault(k, clean(n))
for sid in npcskin:
    n = npcname.get(int(sid)) or npcname.get(int(sid) // 100)
    if n: rows.setdefault(sid, n)
for i, n in npcname.items(): rows.setdefault(str(i), n)
lines = ["# Stella Sora ID -> name lookup used for the Name column.",
         "# Format: <id><TAB><name>. Ids are the number in a mod file name, e.g. char_l2d_14301 -> 14301.",
         "# Built from EN data at https://github.com/Hiro420/StellaSoraData . Edit or add lines freely.", ""]
lines += [f"{k}\t{rows[k]}" for k in sorted(rows, key=int)]
out.write_text("\n".join(lines) + "\n", "utf-8")
print(len(rows), "entries")
