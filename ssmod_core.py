"""
Core logic for the Stella Sora Mod Manager.

Concepts
--------
* Mod file      : a .unity3d bundle in the mod folder whose file name matches a game bundle.
* Original      : the official game bundle. The "newest" original is the copy whose MD5
                  matches the game's manifest (Persistent_Store/ss_win.mani).
* Fingerprint   : {path_id: (type, content_hash)} of every object in a bundle. Texture2D
                  hashes use the real pixel data (inline or streamed from .resS), so mods
                  that store textures differently still compare correctly.
* Port          : rebuild a mod on top of the newest original: only objects the mod really
                  changed are copied in, everything else comes from the newest game version.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import UnityPy

Log = Callable[[str], None]

BACKUP_DIR_NAME = "Backup"
STATE_FILE = "state.json"
BUNDLE_EXT = ".unity3d"
# Bump when plan() logic changes so cached verdicts in state.json are re-evaluated.
ANALYSIS_VERSION = 3
# Types that hold no references to other objects, so they can be matched by name
# and copied into a bundle whose path IDs were renumbered.
NAME_MATCH_TYPES = ("Texture2D", "TextAsset", "Mesh")


# --------------------------------------------------------------------------- helpers
def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_md5_cache: dict[tuple[str, int, float], str] = {}


def md5_cached(path: Path) -> str:
    st = path.stat()
    key = (str(path).lower(), st.st_size, st.st_mtime)
    if key not in _md5_cache:
        _md5_cache[key] = md5_file(path)
    return _md5_cache[key]


def load_bundle(path: Path):
    """Load from bytes so no file handle stays open (needed to replace files on Windows)."""
    return UnityPy.load(path.read_bytes())


def bundle_file(env):
    return next(iter(env.files.values()))


def cab_names(env) -> list[str]:
    return sorted(bundle_file(env).files.keys())


def texture_raw(obj) -> tuple[dict, bytes]:
    tree = obj.read_typetree()
    data = obj.read().get_image_data()
    return tree, bytes(data)


def fingerprint_env(env) -> dict[str, list]:
    fp: dict[str, list] = {}
    for obj in env.objects:
        t = obj.type.name
        if t == "Texture2D":
            try:
                tree, raw = texture_raw(obj)
                h = hashlib.md5()
                h.update(repr((tree.get("m_Width"), tree.get("m_Height"), tree.get("m_TextureFormat"),
                               tree.get("m_MipCount"))).encode())
                h.update(raw)
                fp[str(obj.path_id)] = [t, h.hexdigest(), tree.get("m_Name", "")]
                continue
            except Exception:
                pass  # fall back to raw object bytes
        name = ""
        if t in ("Texture2D", "TextAsset", "AnimationClip", "Sprite", "Material", "Mesh"):
            try:
                name = obj.read_typetree().get("m_Name", "")
            except Exception:
                pass
        fp[str(obj.path_id)] = [t, hashlib.md5(obj.get_raw_data()).hexdigest(), name]
    return fp


def _unique_names(objects: dict[str, list]) -> dict[tuple[str, str], str]:
    """(type, name) -> path_id for name-matchable objects whose name is unique within the bundle."""
    seen: dict[tuple[str, str], str | None] = {}
    for pid, (t, _h, n) in objects.items():
        if t in NAME_MATCH_TYPES and n:
            seen[(t, n)] = None if (t, n) in seen else pid
    return {k: v for k, v in seen.items() if v is not None}


def match_objects(mo: dict[str, list], oo: dict[str, list]) -> dict[str, str]:
    """mod path_id -> game path_id.

    1. Same path ID (and same type/name).
    2. Name-matchable types whose (type, name) is unique in both bundles.
    3. By elimination: name-matchable objects left over after 1+2 whose (type, name) has exactly
       one unmatched object on each side (e.g. one model's 'texture_00' was renumbered while the
       other models' 'texture_00' kept their IDs).
    """
    m_by_name, o_by_name = _unique_names(mo), _unique_names(oo)
    pairs: dict[str, str] = {}
    for pid, val in mo.items():
        cur = oo.get(pid)
        if cur is not None and not (cur[0] != val[0] or (cur[2] and val[2] and cur[2] != val[2])):
            pairs[pid] = pid
    used = set(pairs.values())
    for pid, val in mo.items():
        if pid in pairs or val[0] not in NAME_MATCH_TYPES or m_by_name.get((val[0], val[2])) != pid:
            continue
        gpid = o_by_name.get((val[0], val[2]))
        if gpid and gpid not in used:
            pairs[pid] = gpid
            used.add(gpid)
    left_m: dict[tuple[str, str], list[str]] = {}
    left_o: dict[tuple[str, str], list[str]] = {}
    for pid, (t, _h, n) in mo.items():
        if pid not in pairs and t in NAME_MATCH_TYPES and n:
            left_m.setdefault((t, n), []).append(pid)
    for pid, (t, _h, n) in oo.items():
        if pid not in used and t in NAME_MATCH_TYPES and n:
            left_o.setdefault((t, n), []).append(pid)
    for key, mp in left_m.items():
        op = left_o.get(key, [])
        if len(mp) == 1 and len(op) == 1:
            pairs[mp[0]] = op[0]
    return pairs


def is_game_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq StellaSora.exe", "/NH"],
                             capture_output=True, text=True, creationflags=0x08000000).stdout
        return "stellasora.exe" in out.lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- game
class Game:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.persistent = self.root / "Persistent_Store" / "AssetBundles"
        self.install = self.root / "StellaSora_Data" / "StreamingAssets" / "InstallResource"
        self.manifest: dict[str, str] = {}
        self.info: dict[str, str] = {}
        self.reload_manifest()

    def valid(self) -> bool:
        return (self.root / "StellaSora.exe").exists() or self.persistent.exists() or self.install.exists()

    def reload_manifest(self):
        self.manifest.clear()
        self.info.clear()
        mani = self.root / "Persistent_Store" / "ss_win.mani"
        if not mani.exists():
            return
        for line in mani.read_text("utf-8", errors="replace").splitlines():
            if line.startswith("$"):
                k, _, v = line[1:].partition(":")
                self.info[k] = v
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[0].endswith(BUNDLE_EXT):
                self.manifest[parts[0].lower()] = parts[1].lower()

    @property
    def version_text(self) -> str:
        gv = self.info.get("GAME_VER", "?")
        rv = self.info.get("RES_VER", "?")
        return f"Game {gv}  ·  Resources {rv}"

    def candidates(self, name: str) -> list[Path]:
        return [p for p in (self.persistent / name, self.install / name) if p.exists()]

    def active_path(self, name: str, recorded: str | None = None) -> Path | None:
        """The copy of `name` the game actually loads."""
        if recorded and Path(recorded).exists():
            return Path(recorded)
        cands = self.candidates(name)
        want = self.manifest.get(name.lower())
        if want:
            for p in cands:
                if md5_cached(p) == want:
                    return p
        return cands[0] if cands else None  # Persistent_Store overrides InstallResource


# --------------------------------------------------------------------------- state
@dataclass
class ModStatus:
    name: str
    mod_path: Path
    mod_md5: str = ""
    game_path: Path | None = None
    location: str = ""
    code: str = "unknown"
    text: str = "Not checked"
    detail: str = ""

    @property
    def actionable(self) -> bool:
        return self.code in ("ready", "port", "reinstall", "game_updated", "unchecked")


STATUS_TEXT = {
    "installed": "Installed ✓",
    "ready": "Fits newest version – ready",
    "port": "Outdated – needs update",
    "reinstall": "Mod file changed – reinstall",
    "game_updated": "Game updated – reinstall",
    "identical": "No changes vs game",
    "missing": "Not found in game",
    "incompatible": "Incompatible",
    "unchecked": "Not installed",
    "error": "Error",
}


class Manager:
    def __init__(self, mod_dir: Path, game_dir: Path, log: Log = print, backup_dir: Path | None = None):
        self.mod_dir = Path(mod_dir)
        self.game = Game(Path(game_dir))
        self.log = log
        self.backup_dir = Path(backup_dir) if backup_dir else self.mod_dir / BACKUP_DIR_NAME
        self.orig_dir = self.backup_dir / "originals"
        self.old_mod_dir = self.backup_dir / "old_mods"
        self.cache_dir = self.backup_dir / "cache"
        self.state_path = self.backup_dir / STATE_FILE
        self.state = self._load_state()

    # ---- persistence
    def _load_state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            s = {}
        s.setdefault("installed", {})
        s.setdefault("mods", {})
        if s.get("analysis_version", 1) < ANALYSIS_VERSION:
            # "identical" verdicts from older analysis could be wrong (renumbered path IDs) – recheck them.
            for info in s["mods"].values():
                info["identical"] = []
            s["analysis_version"] = ANALYSIS_VERSION
        return s

    def save_state(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), "utf-8")
        os.replace(tmp, self.state_path)

    def mod_info(self, md5: str) -> dict:
        return self.state["mods"].setdefault(md5, {"fits": [], "identical": []})

    # ---- fingerprints (cached on disk by md5)
    def fingerprint(self, path: Path, md5: str | None = None) -> dict:
        md5 = md5 or md5_cached(path)
        cache = self.cache_dir / f"{md5}.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text("utf-8"))
            except Exception:
                pass
        env = load_bundle(path)
        data = {"cabs": cab_names(env), "objects": fingerprint_env(env)}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), "utf-8")
        return data

    # ---- discovery
    def list_mods(self) -> list[Path]:
        if not self.mod_dir.exists():
            self.mod_dir.mkdir(parents=True, exist_ok=True)
            return []
        found: dict[str, Path] = {}
        for p in sorted(self.mod_dir.rglob(f"*{BUNDLE_EXT}")):
            if self.backup_dir in p.parents or p.name.startswith("~"):
                continue
            key = p.name.lower()
            if key in found:
                self.log(f"⚠ Duplicate mod '{p.name}' in {p.parent} – using {found[key].parent}")
                continue
            found[key] = p
        return list(found.values())

    def location_label(self, p: Path | None) -> str:
        if p is None:
            return "-"
        return "Persistent_Store" if self.game.persistent in p.parents else "InstallResource"

    # ---- status
    def status(self, mod_path: Path, deep: bool = False) -> ModStatus:
        name = mod_path.name
        st = ModStatus(name=name, mod_path=mod_path)
        try:
            st.mod_md5 = md5_cached(mod_path)
            rec = self.state["installed"].get(name.lower())
            gp = self.game.active_path(name, rec["game_path"] if rec else None)
            st.game_path, st.location = gp, self.location_label(gp)
            if gp is None:
                return self._set(st, "missing", "No bundle with this name in the game folders.")
            gmd5 = md5_cached(gp)
            if rec:
                if gmd5 == rec["installed_md5"]:
                    if rec["mod_md5"] == st.mod_md5:
                        return self._set(st, "installed", f"Backup of original: {rec['original_md5'][:8]}")
                    return self._set(st, "reinstall", "The mod file in the mod folder is different from the installed one.")
                return self._set(st, "game_updated", "The game replaced the modded file (update or repair).")
            info = self.state["mods"].get(st.mod_md5, {})
            if gmd5 in info.get("fits", []):
                return self._set(st, "ready", "Already verified against this game version.")
            if gmd5 in info.get("identical", []):
                return self._set(st, "identical", "Mod content equals the current game file – nothing to install.")
            if not deep:
                return self._set(st, "unchecked", "Not yet checked against the current game version.")
            plan = self.plan(mod_path, gp)
            return self._set(st, plan["code"], plan["summary"])
        except Exception as e:  # noqa: BLE001
            return self._set(st, "error", f"{type(e).__name__}: {e}")

    @staticmethod
    def _set(st: ModStatus, code: str, detail: str) -> ModStatus:
        st.code, st.text, st.detail = code, STATUS_TEXT.get(code, code), detail
        return st

    # ---- analysis
    def known_old_originals(self, name: str, exclude_md5: str) -> list[Path]:
        """Other official versions of this bundle we know about (help separate mod edits from game updates)."""
        out, seen = [], {exclude_md5}
        rec = self.state["installed"].get(name.lower())
        for p in sorted((self.orig_dir / name).glob(f"*{BUNDLE_EXT}")) if (self.orig_dir / name).exists() else []:
            m = p.stem.split(".")[-1]
            if m not in seen:
                seen.add(m)
                out.append(p)
        for p in self.game.candidates(name):
            m = md5_cached(p)
            if m in seen or (rec and m == rec.get("installed_md5")):
                continue
            seen.add(m)
            out.append(p)
        return out

    def plan(self, mod_path: Path, original: Path) -> dict:
        """Compare a mod with the newest original. Returns code ready|port|identical|incompatible."""
        name = mod_path.name
        mod_md5, orig_md5 = md5_cached(mod_path), md5_cached(original)
        self.log(f"  Analysing {name} against game file {orig_md5[:8]} …")
        mfp, ofp = self.fingerprint(mod_path, mod_md5), self.fingerprint(original, orig_md5)
        if mfp["cabs"] != ofp["cabs"]:
            return {"code": "incompatible", "summary": "Internal bundle name differs from the game's – not the same asset.",
                    "edits": [], "warnings": []}
        olds = [self.fingerprint(p)["objects"] for p in self.known_old_originals(name, orig_md5)]
        mo, oo = mfp["objects"], ofp["objects"]
        # Game updates can renumber path IDs. Objects without references to other objects
        # can then be matched by (type, name) – see match_objects().
        pairs = match_objects(mo, oo)
        game_hashes = {(v[0], v[1]) for v in oo.values()}
        edits: list[list[str]] = []  # [mod_pid, game_pid]
        matched_game: set[str] = set()
        game_changes, warnings, unmatched, lost, unsure = 0, [], 0, [], []
        for pid, val in mo.items():
            gpid = pairs.get(pid)
            cur = oo.get(gpid) if gpid else None
            if cur is None:
                unmatched += 1
                if (val[0], val[1]) not in game_hashes and (val[2] or val[0] in NAME_MATCH_TYPES):
                    lost.append(f"{val[0]} '{val[2] or pid}'")
                continue
            matched_game.add(gpid)
            if cur[:2] == val[:2]:
                continue
            if any(o.get(pid, [None, None])[:2] == val[:2] for o in olds):
                game_changes += 1  # mod still carries an older official version of this object
                continue
            if val[0] == "Texture2D" or olds:
                edits.append([pid, gpid])
            else:
                game_changes += 1
                unsure.append(f"{val[0]} '{val[2]}'" if val[2] else val[0])
        if unsure:
            counts: dict[str, int] = {}
            for u in unsure:
                counts[u] = counts.get(u, 0) + 1
            warnings.append("Differs but can't tell if mod or game changed it – kept game version: "
                            + ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in sorted(counts.items())))
        if unmatched:
            warnings.append(f"{unmatched} object(s) of the mod have no counterpart in the game file – skipped")
        if lost:
            warnings.append("Not carried over (older game data or mod edits that can't be matched): "
                            + ", ".join(sorted(lost)[:20])
                            + (" …" if len(lost) > 20 else ""))
        missing = [pid for pid in oo if pid not in matched_game]
        game_changes += len(missing)
        names = ", ".join(sorted({mo[m][2] or m for m, _ in edits}))
        if not edits and lost:
            code, summary = "incompatible", ("The game file was restructured and the mod's changes could not be "
                                             f"matched to it ({len(lost)} object(s)). The mod needs a new version.")
        elif not edits:
            code, summary = "identical", "Mod content equals the current game file – nothing to install."
        elif game_changes == 0:
            code, summary = "ready", f"Fits newest version. Modded: {names}"
        else:
            code, summary = "port", f"Built for an older game version ({game_changes} game change(s)). Modded: {names}"
        for w in warnings:
            self.log(f"  ⚠ {w}")
        info = self.mod_info(mod_md5)
        if code == "ready" and orig_md5 not in info["fits"]:
            info["fits"].append(orig_md5)
        if code == "identical" and orig_md5 not in info["identical"]:
            info["identical"].append(orig_md5)
        self.save_state()
        return {"code": code, "summary": summary, "edits": edits, "warnings": warnings}

    # ---- porting
    def port(self, mod_path: Path, original: Path, edits: list[list[str]]) -> bytes:
        """`edits` is a list of [mod_path_id, game_path_id] pairs."""
        self.log(f"  Rebuilding {mod_path.name} on top of the newest game version ({len(edits)} modded object(s)) …")
        env = load_bundle(original)
        menv = load_bundle(mod_path)
        mobjs = {str(o.path_id): o for o in menv.objects}
        want = {g: m for m, g in edits}  # game pid -> mod pid
        for obj in env.objects:
            pid = str(obj.path_id)
            if pid not in want:
                continue
            src = mobjs[want[pid]]
            if src.type.name == "Texture2D":
                tree, raw = texture_raw(src)
                tree["image data"] = raw
                tree["m_StreamData"] = {"offset": 0, "size": 0, "path": ""}
                tree["m_CompleteImageSize"] = len(raw) if tree.get("m_MipCount", 1) == 1 else tree.get("m_CompleteImageSize", len(raw))
                obj.save_typetree(tree)
            else:
                obj.set_raw_data(src.get_raw_data())
        data = bundle_file(env).save(packer="original")  # same compression/flags as the game's file
        # verify the result before anyone uses it
        check = fingerprint_env(UnityPy.load(data))
        mfp = self.fingerprint(mod_path)["objects"]
        ofp = self.fingerprint(original)["objects"]
        for pid, val in check.items():
            expect = mfp[want[pid]] if pid in want else ofp.get(pid)
            if expect is None or expect[:2] != val[:2]:
                raise RuntimeError(f"Verification failed for object {pid} ({val[0]})")
        if len(check) != len(ofp):
            raise RuntimeError("Verification failed: object count changed")
        return data

    # ---- backups
    def backup_original(self, name: str, path: Path) -> str:
        md5 = md5_cached(path)
        dest = self.orig_dir / name / f"{Path(name).stem}.{md5}{BUNDLE_EXT}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            shutil.copy2(path, tmp)
            if md5_file(tmp) != md5:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("Backup copy is corrupt")
            os.replace(tmp, dest)
            self.log(f"  Backed up original → Backup\\originals\\{name}\\{dest.name}")
        return md5

    def backup_path(self, name: str, md5: str) -> Path:
        return self.orig_dir / name / f"{Path(name).stem}.{md5}{BUNDLE_EXT}"

    @staticmethod
    def write_atomic(dest: Path, data: bytes | Path):
        tmp = dest.with_name(dest.name + ".ssmm_tmp")
        if isinstance(data, Path):
            shutil.copyfile(data, tmp)
        else:
            tmp.write_bytes(data)
        for i in range(5):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                time.sleep(0.5 * (i + 1))
        tmp.unlink(missing_ok=True)
        raise PermissionError(f"Could not write {dest} (is the game running?)")

    # ---- actions
    def _original_for(self, name: str, backup: bool) -> tuple[Path | None, Path | None]:
        """(game file to replace, pristine original to build on)."""
        key = name.lower()
        rec = self.state["installed"].get(key)
        gp = self.game.active_path(name, rec["game_path"] if rec else None)
        if gp is None:
            return None, None
        gmd5 = md5_cached(gp)
        if rec and gmd5 == rec["installed_md5"]:
            original = self.backup_path(name, rec["original_md5"])
            if not original.exists():
                raise RuntimeError(f"Backup of original is missing: {original}")
            return gp, original
        if not backup:
            return gp, gp  # game file is the original; only read it
        want = self.game.manifest.get(key)
        if want and gmd5 != want:
            self.log("  ⚠ Game file does not match the game manifest (already modded by something else?). "
                     "It will be backed up as-is.")
        self.backup_original(name, gp)
        return gp, self.backup_path(name, gmd5)

    def _ensure_fits(self, mod_path: Path, original: Path) -> str | None:
        """Make the mod file in the mod folder fit `original` (rebuild if outdated). Returns its md5, or None."""
        orig_md5 = md5_cached(original)
        mod_md5 = md5_cached(mod_path)
        info = self.state["mods"].get(mod_md5, {})
        if orig_md5 in info.get("identical", []):
            self.log("  Mod is identical to the game file – nothing to do.")
            return None
        if orig_md5 in info.get("fits", []):
            return mod_md5
        plan = self.plan(mod_path, original)
        self.log(f"  {STATUS_TEXT[plan['code']]}: {plan['summary']}")
        if plan["code"] in ("identical", "incompatible"):
            return None
        if plan["code"] == "port":
            data = self.port(mod_path, original, plan["edits"])
            self.old_mod_dir.mkdir(parents=True, exist_ok=True)
            old = self.old_mod_dir / f"{mod_path.stem}.{mod_md5}{BUNDLE_EXT}"
            if not old.exists():
                shutil.copy2(mod_path, old)
            self.write_atomic(mod_path, data)
            mod_md5 = md5_cached(mod_path)
            self.mod_info(mod_md5)["fits"] = [orig_md5]
            self.save_state()
            self.log(f"  ✓ Updated mod saved to Mods (old version → Backup\\old_mods\\{old.name})")
        return mod_md5

    def update_mod(self, mod_path: Path) -> ModStatus:
        """Check the mod and rebuild it for the newest game version if needed. Never touches the game."""
        self.log(f"▶ {mod_path.name}")
        gp, original = self._original_for(mod_path.name, backup=False)
        if gp is None:
            self.log("  Not found in the game folders – skipped.")
        else:
            self._ensure_fits(mod_path, original)
        st = self.status(mod_path)
        self.log(f"  → {st.text}")
        return st

    def install(self, mod_path: Path) -> ModStatus:
        name = mod_path.name
        self.log(f"▶ {name}")
        gp, original = self._original_for(name, backup=True)
        if gp is None:
            self.log("  Not found in the game folders – skipped.")
            return self.status(mod_path)
        mod_md5 = self._ensure_fits(mod_path, original)
        if mod_md5 is None:
            return self.status(mod_path)
        self.write_atomic(gp, mod_path)
        inst_md5 = md5_cached(gp)
        if inst_md5 != mod_md5:
            raise RuntimeError("Copied file does not match the mod (disk error?)")
        self.state["installed"][name.lower()] = {
            "name": name, "game_path": str(gp), "original_md5": md5_cached(original),
            "installed_md5": inst_md5, "mod_md5": mod_md5, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_state()
        self.log(f"  Installed → {self.location_label(gp)}\\{name}")
        return self.status(mod_path)

    def revert(self, name: str) -> bool:
        key = name.lower()
        rec = self.state["installed"].get(key)
        if not rec:
            return False
        gp = Path(rec["game_path"])
        self.log(f"◀ Reverting {name}")
        if gp.exists() and md5_cached(gp) != rec["installed_md5"]:
            self.log("  Game file was already replaced by the game – leaving it alone.")
        else:
            src = self.backup_path(name, rec["original_md5"])
            if not src.exists():
                self.log(f"  ✖ Backup missing ({src}). Use Steam 'Verify integrity' to restore this file.")
                return False
            self.write_atomic(gp, src)
            self.log(f"  Restored original → {self.location_label(gp)}\\{name}")
        del self.state["installed"][key]
        self.save_state()
        return True

    def installed_names(self) -> list[str]:
        return [r.get("name", k) for k, r in self.state["installed"].items()]
