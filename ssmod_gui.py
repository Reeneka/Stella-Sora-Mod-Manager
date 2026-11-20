import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QLockFile, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import ssmod_core as core

APP_NAME = "Stella Sora Mod Manager"
DEFAULT_GAME_DIR = r"D:\SteamLibrary\steamapps\common\Stella Sora"
# Everything lives next to the exe:  <manager>\Mods, <manager>\Backup, <manager>\config.json
APP_HOME = Path(os.environ.get("SSMM_HOME") or
                (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent))
MOD_DIR = APP_HOME / "Mods"
BACKUP_DIR = APP_HOME / core.BACKUP_DIR_NAME
CONFIG_PATH = APP_HOME / "config.json"

STATUS_COLORS = {
    "installed": "#5bd68a", "ready": "#7cc4ff", "port": "#ffc861", "reinstall": "#ffc861",
    "game_updated": "#ffc861", "identical": "#9aa0b4", "missing": "#ff7b7b", "incompatible": "#ff7b7b",
    "error": "#ff7b7b", "unchecked": "#d8dcef",
}

STYLE = """
QWidget { background: #171a24; color: #e4e7f2; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel#title { font-size: 17pt; font-weight: 600; color: #ffffff; }
QLabel#sub { color: #9aa0b4; }
QLabel#pill { background: #252a3a; border-radius: 10px; padding: 3px 10px; color: #c9cfe6; }
QLineEdit { background: #20243200; background-color: #202433; border: 1px solid #2f3548; border-radius: 6px; padding: 5px 8px; }
QPushButton { background: #2a3045; border: 1px solid #353c55; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #343b55; }
QPushButton:disabled { color: #6b7088; background: #20243300; border-color: #2a2f40; }
QPushButton#primary { background: #4f7cff; border-color: #4f7cff; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #6a90ff; }
QPushButton#danger { background: #3a2530; border-color: #5a3040; color: #ffb3c0; }
QPushButton#danger:hover { background: #4a2d3b; }
QTableWidget { background: #1c2030; border: 1px solid #2a2f40; border-radius: 8px; gridline-color: #262b3c;
               selection-background-color: #2f3b63; selection-color: #ffffff; }
QHeaderView::section { background: #1f2333; color: #9aa0b4; border: none; border-bottom: 1px solid #2a2f40; padding: 6px; }
QPlainTextEdit { background: #11131b; border: 1px solid #2a2f40; border-radius: 8px; font-family: Consolas; font-size: 9pt; }
QProgressBar { background: #20243300; border: 1px solid #2a2f40; border-radius: 5px; height: 10px; text-align: center; }
QProgressBar::chunk { background: #4f7cff; border-radius: 5px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QSplitter::handle { background: #171a24; }
"""


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", Path(__file__).parent)
    return str(Path(base) / name)


def load_names() -> dict[str, str]:
    """ID -> character/NPC name from names.txt (next to the exe first, else the bundled copy)."""
    names = {}
    for p in (APP_HOME / "names.txt", Path(resource("names.txt"))):
        try:
            for line in p.read_text("utf-8").splitlines():
                if line and not line.startswith("#") and "\t" in line:
                    k, v = line.split("\t", 1)
                    names.setdefault(k.strip(), v.strip())
        except OSError:
            continue
    return names


NAMES = load_names()


def mod_display_name(filename: str) -> str:
    """char_l2d_14301.unity3d -> 'Wraith'. Unknown skins fall back to the base character/NPC id."""
    digits = re.search(r"(\d+)$", Path(filename).stem)
    if not digits:
        return "-"
    i = digits.group(1)
    return NAMES.get(i) or NAMES.get(i[:-2]) or "-"


def load_config() -> dict:
    cfg = {"game_dir": "", "auto": True, "first_run": not CONFIG_PATH.exists()}
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    except Exception:
        cfg["first_run"] = True
    cfg.pop("mod_dir", None)
    if not cfg["game_dir"]:
        cfg["game_dir"] = DEFAULT_GAME_DIR
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
    except Exception:
        pass


class Worker(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    row = Signal(object)
    done = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.fn(self)
            self.done.emit("")
        except Exception as e:  # noqa: BLE001
            self.log.emit("✖ " + "".join(traceback.format_exception_only(type(e), e)).strip())
            self.done.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.mgr: core.Manager | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.rows: dict[str, int] = {}
        self.statuses: dict[str, core.ModStatus] = {}
        self.last_sizes: dict[str, int] = {}

        self.setWindowTitle(APP_NAME)
        self.resize(1080, 720)
        if Path(resource("icon.ico")).exists():
            self.setWindowIcon(QIcon(resource("icon.ico")))

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        # header
        head = QHBoxLayout()
        tl = QVBoxLayout()
        t = QLabel(APP_NAME)
        t.setObjectName("title")
        s = QLabel("Add mods (button or drag & drop onto this window) – they are checked, updated for the newest game version, backed up and installed.")
        s.setObjectName("sub")
        tl.addWidget(t)
        tl.addWidget(s)
        head.addLayout(tl, 1)
        self.version_lbl = QLabel("…")
        self.version_lbl.setObjectName("pill")
        self.running_lbl = QLabel("")
        self.running_lbl.setObjectName("pill")
        head.addWidget(self.running_lbl, 0, Qt.AlignTop)
        head.addWidget(self.version_lbl, 0, Qt.AlignTop)
        lay.addLayout(head)

        # paths
        self.mod_edit = self._path_row(lay, "Mod folder", str(MOD_DIR), browse=False)
        self.game_edit = self._path_row(lay, "Game folder", self.cfg["game_dir"])

        # toolbar
        bar = QHBoxLayout()
        self.btn_add = QPushButton("＋  Add mods…")
        self.btn_check = QPushButton("⟳  Check && update mods")
        self.btn_check.setToolTip("Check every mod against the newest game version and rebuild outdated ones "
                                  "in the Mods folder. Does not change the game.")
        self.btn_install_sel = QPushButton("Install / update selected")
        self.btn_install_all = QPushButton("Install / update all")
        self.btn_install_all.setObjectName("primary")
        self.btn_revert_sel = QPushButton("Revert selected")
        self.btn_revert_sel.setObjectName("danger")
        self.btn_revert_all = QPushButton("Revert ALL to original")
        self.btn_revert_all.setObjectName("danger")
        for b in (self.btn_add, self.btn_check, self.btn_install_sel, self.btn_install_all):
            bar.addWidget(b)
        bar.addStretch(1)
        for b in (self.btn_revert_sel, self.btn_revert_all):
            bar.addWidget(b)
        lay.addLayout(bar)

        # table + log
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Mod file", "Game location", "Name", "Status", "Details"])
        hh = self.table.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        split.addWidget(self.table)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(5000)
        split.addWidget(self.logbox)
        split.setSizes([400, 220])
        lay.addWidget(split, 1)

        # footer
        foot = QHBoxLayout()
        self.auto_chk = QCheckBox("Auto-install new mods and re-apply after game updates")
        self.auto_chk.setChecked(bool(self.cfg.get("auto", True)))
        foot.addWidget(self.auto_chk)
        foot.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        self.progress.hide()
        foot.addWidget(self.progress)
        b1 = QPushButton("Open mod folder")
        b2 = QPushButton("Open backup folder")
        b1.clicked.connect(lambda: self._open(self.mod_edit.text()))
        b2.clicked.connect(lambda: self._open(str(BACKUP_DIR)))
        foot.addWidget(b1)
        foot.addWidget(b2)
        lay.addLayout(foot)

        self.btn_add.clicked.connect(self.add_mods_dialog)
        self.setAcceptDrops(True)
        self.btn_check.clicked.connect(lambda: self.run_check(None))
        self.btn_install_sel.clicked.connect(lambda: self.run_install(self.selected_names()))
        self.btn_install_all.clicked.connect(lambda: self.run_install(None))
        self.btn_revert_sel.clicked.connect(lambda: self.run_revert(self.selected_names()))
        self.btn_revert_all.clicked.connect(lambda: self.run_revert(None))
        self.auto_chk.toggled.connect(self._auto_toggled)
        self.game_edit.editingFinished.connect(self.paths_changed)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._fs_changed)
        self.watcher.fileChanged.connect(self._fs_changed)
        self.debounce = QTimer(self, singleShot=True, interval=2500)
        self.debounce.timeout.connect(self.on_fs_settled)
        self.run_timer = QTimer(self, interval=5000)
        self.run_timer.timeout.connect(self._update_running)
        self.run_timer.start()
        self.pending_auto = False

        self.log(f"{APP_NAME} started.")
        QTimer.singleShot(100, self.paths_changed)

    # ---------------------------------------------------------------- ui helpers
    def _path_row(self, lay, label, value, browse=True):
        row = QHBoxLayout()
        l = QLabel(label)
        l.setFixedWidth(90)
        e = QLineEdit(value)
        row.addWidget(l)
        row.addWidget(e, 1)
        if browse:
            b = QPushButton("Browse…")
            b.clicked.connect(lambda: self._browse(e))
            row.addWidget(b)
        else:
            e.setReadOnly(True)
            e.setToolTip("Put your .unity3d mods here")
            b = QPushButton("Open")
            b.clicked.connect(lambda: self._open(e.text()))
            row.addWidget(b)
        lay.addLayout(row)
        return e

    def _browse(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if d:
            edit.setText(os.path.normpath(d))
            self.paths_changed()

    @staticmethod
    def _open(path):
        Path(path).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def log(self, msg: str):
        self.logbox.appendPlainText(time.strftime("[%H:%M:%S] ") + msg)

    def busy(self) -> bool:
        return self.thread is not None

    def set_busy(self, on: bool):
        for b in (self.btn_add, self.btn_check, self.btn_install_sel, self.btn_install_all, self.btn_revert_sel, self.btn_revert_all):
            b.setEnabled(not on)
        self.game_edit.setEnabled(not on)
        self.progress.setVisible(on)
        self.progress.setRange(0, 0)

    def selected_names(self) -> list[str]:
        names = sorted({self.table.item(i.row(), 0).text() for i in self.table.selectedIndexes()})
        if not names:
            QMessageBox.information(self, APP_NAME, "Select one or more mods in the list first.")
        return names

    def _auto_toggled(self, on):
        self.cfg["auto"] = on
        save_config(self.cfg)
        if on:
            self.pending_auto = True
            self.debounce.start()

    def _update_running(self):
        running = core.is_game_running()
        self.running_lbl.setText("● Game running" if running else "")
        self.running_lbl.setVisible(running)
        if not running and self.pending_auto and self.auto_chk.isChecked() and not self.busy():
            self.debounce.start()
        return running

    # ---------------------------------------------------------------- paths / watcher
    def paths_changed(self):
        if self.busy():
            return
        mod_dir, game_dir = MOD_DIR, Path(self.game_edit.text().strip())
        mod_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.update(game_dir=str(game_dir))
        save_config(self.cfg)
        self.mgr = core.Manager(mod_dir, game_dir, log=lambda m: self.worker.log.emit(m) if self.worker else self.log(m),
                                backup_dir=BACKUP_DIR)
        if not self.mgr.game.valid():
            self.version_lbl.setText("Game folder not found")
            self.log("⚠ The game folder doesn't look right – choose the 'Stella Sora' folder inside steamapps\\common.")
        else:
            self.version_lbl.setText(self.mgr.game.version_text)
        if not mod_dir.exists():
            self.log("⚠ Mod folder does not exist.")
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        for d in (mod_dir, self.mgr.game.persistent, self.mgr.game.install, self.mgr.game.root / "Persistent_Store"):
            if d.exists():
                self.watcher.addPath(str(d))
        if self.cfg.pop("first_run", False):
            # never touch the game before the user has seen the folders once
            save_config(self.cfg)
            self.refresh(then_auto=False)
            self.log("First start: check the two folders above, then press 'Install / update all'. "
                     "From now on new mods and game updates are handled automatically (checkbox below).")
            return
        self.pending_auto = True
        self.refresh(then_auto=True)

    def _fs_changed(self, _path):
        if not self.busy():
            self.debounce.start()

    def on_fs_settled(self):
        if self.busy():
            self.debounce.start()
            return
        # wait until files stop growing (a mod still being copied)
        sizes = {}
        for p in self.mgr.list_mods():
            try:
                sizes[str(p)] = p.stat().st_size
            except OSError:
                pass
        if sizes != self.last_sizes:
            self.last_sizes = sizes
            self.debounce.start()
            return
        self.mgr.game.reload_manifest()
        self.version_lbl.setText(self.mgr.game.version_text)
        self.refresh(then_auto=True)

    # ---------------------------------------------------------------- table
    def refresh(self, then_auto=False):
        if not self.mgr:
            return
        mods = self.mgr.list_mods()
        self.table.setRowCount(0)
        self.rows.clear()
        self.statuses.clear()
        for p in mods:
            st = self.mgr.status(p)
            self.set_row(st)
        for name in self.mgr.installed_names():  # installed mods whose file left the mod folder
            if name.lower() not in {p.name.lower() for p in mods}:
                st = core.ModStatus(name=name, mod_path=Path(), code="orphan", text="Installed (mod file removed)",
                                    detail="Use Revert to restore the original.")
                st.location = self.mgr.location_label(Path(self.mgr.state["installed"][name.lower()]["game_path"]))
                self.set_row(st)
        if not mods:
            self.log(f"No .unity3d mods found in {self.mgr.mod_dir}")
        if then_auto and self.auto_chk.isChecked():
            todo = [s.name for s in self.statuses.values() if s.actionable]
            if todo:
                if core.is_game_running():
                    self.pending_auto = True
                    self.log(f"Auto-install waiting: {len(todo)} mod(s) pending – close the game first.")
                    return
                self.pending_auto = False
                self.log(f"Auto-install: {len(todo)} mod(s) need attention.")
                self.run_install(todo, confirm=False)
                return
            self.pending_auto = False

    def set_row(self, st: core.ModStatus):
        self.statuses[st.name] = st
        r = self.rows.get(st.name)
        if r is None:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.rows[st.name] = r
        vals = [st.name, st.location or "-", mod_display_name(st.name), st.text, st.detail]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(v)
            it.setToolTip(v)
            if c == 3:
                it.setForeground(QColor(STATUS_COLORS.get(st.code, "#d8dcef")))
                f = QFont()
                f.setBold(True)
                it.setFont(f)
            self.table.setItem(r, c, it)

    # ---------------------------------------------------------------- jobs
    def start_job(self, fn, title):
        if self.busy():
            return
        self.log(title)
        self.set_busy(True)
        self.thread = QThread(self)
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.row.connect(self.set_row)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._job_done)
        self.thread.start()

    def _progress(self, i, n):
        self.progress.setRange(0, n)
        self.progress.setValue(i)

    def _job_done(self, err):
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self.set_busy(False)
        self.log("Done." if not err else "Finished with errors – see above.")
        self.refresh(then_auto=False)
        self.last_sizes = {}

    def _mods_by_name(self, names):
        mods = {p.name: p for p in self.mgr.list_mods()}
        return [mods[n] for n in names if n in mods] if names is not None else list(mods.values())

    def _guard_game(self) -> bool:
        if core.is_game_running():
            QMessageBox.warning(self, APP_NAME, "Stella Sora is running. Close the game first so its files can be replaced.")
            return False
        return True

    def run_check(self, names):
        if self.busy() or not self.mgr:
            return
        mods = self._mods_by_name(names)
        mgr = self.mgr

        def job(w: Worker):
            mgr.game.reload_manifest()
            errors = 0
            for i, p in enumerate(mods):
                w.progress.emit(i, len(mods))
                try:
                    st = mgr.status(p, deep=True)
                    w.log.emit(f"{st.name}: {st.text} – {st.detail}")
                    if st.code == "port":  # rebuild the mod file in Mods for the newest game version
                        st = mgr.update_mod(p)
                    w.row.emit(st)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    w.log.emit(f"  ✖ {p.name}: {type(e).__name__}: {e}")
            w.progress.emit(len(mods), len(mods))
            if errors:
                raise RuntimeError(f"{errors} mod(s) failed")
        self.start_job(job, f"Checking {len(mods)} mod(s) and updating outdated ones (the game is not touched) …")

    def run_install(self, names, confirm=True):
        if self.busy() or not self.mgr or (names is not None and not names):
            return
        if not self._guard_game():
            return
        mods = self._mods_by_name(names)
        if confirm and not mods:
            return
        mgr = self.mgr

        def job(w: Worker):
            mgr.game.reload_manifest()
            errors = 0
            for i, p in enumerate(mods):
                w.progress.emit(i, len(mods))
                try:
                    st = mgr.status(p)
                    if st.code == "installed":
                        w.log.emit(f"✓ {p.name} already installed and up to date.")
                    else:
                        st = mgr.install(p)
                    w.row.emit(st)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    w.log.emit(f"  ✖ {p.name}: {type(e).__name__}: {e}")
                    st = mgr.status(p)
                    mgr._set(st, "error", str(e))
                    w.row.emit(st)
            w.progress.emit(len(mods), len(mods))
            if errors:
                raise RuntimeError(f"{errors} mod(s) failed")
        self.start_job(job, f"Installing {len(mods)} mod(s) …")

    def run_revert(self, names):
        if self.busy() or not self.mgr or (names is not None and not names):
            return
        targets = names if names is not None else self.mgr.installed_names()
        targets = [n for n in targets if n.lower() in self.mgr.state["installed"]]
        if not targets:
            QMessageBox.information(self, APP_NAME, "Nothing to revert – none of these mods are installed.")
            return
        if names is None and QMessageBox.question(
                self, APP_NAME, f"Restore the original game files for all {len(targets)} installed mod(s)?") != QMessageBox.Yes:
            return
        if not self._guard_game():
            return
        if self.auto_chk.isChecked():
            self.auto_chk.setChecked(False)
            self.log("Auto-install turned off so reverted mods are not re-installed. Turn it back on any time.")
        mgr = self.mgr

        def job(w: Worker):
            for i, n in enumerate(targets):
                w.progress.emit(i, len(targets))
                mgr.revert(n)
            w.progress.emit(len(targets), len(targets))
        self.start_job(job, f"Reverting {len(targets)} mod(s) to the original files …")

    # ---------------------------------------------------------------- adding mods
    def add_mods_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose mod files", str(Path.home() / "Downloads"),
                                                "Unity bundles (*.unity3d);;All files (*)")
        if files:
            self.add_mods([Path(f) for f in files])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.busy():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            paths += sorted(p.rglob("*.unity3d")) if p.is_dir() else [p]
        self.add_mods(paths)

    def add_mods(self, paths):
        if self.busy():
            return
        MOD_DIR.mkdir(parents=True, exist_ok=True)
        game_names = None
        added = 0
        for src in paths:
            if src.suffix.lower() != ".unity3d" or not src.is_file():
                self.log(f"Skipped {src.name}: not a .unity3d mod file.")
                continue
            if MOD_DIR in src.parents:
                continue
            if self.mgr and not self.mgr.game.candidates(src.name):
                if QMessageBox.question(self, APP_NAME, f"{src.name} doesn't match any file in the game.\n"
                                        "Add it anyway?") != QMessageBox.Yes:
                    continue
            dest = MOD_DIR / src.name
            if dest.exists():
                if QMessageBox.question(self, APP_NAME, f"A mod named {src.name} already exists.\n"
                                        "Replace it? (the old one is kept in Backup\\old_mods)") != QMessageBox.Yes:
                    continue
                old_dir = BACKUP_DIR / "old_mods"
                old_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, old_dir / f"{dest.stem}.{core.md5_file(dest)}.unity3d")
            shutil.copy2(src, dest.with_name(dest.name + ".ssmm_tmp"))
            os.replace(dest.with_name(dest.name + ".ssmm_tmp"), dest)
            self.log(f"Added {src.name} to Mods.")
            added += 1
        if added:
            self.pending_auto = True
            self.last_sizes = {}
            if self.auto_chk.isChecked():
                self.debounce.start()
            else:
                self.refresh()
                self.log("Auto-install is off – press 'Install / update' to install the new mod(s).")

    def closeEvent(self, e):
        if self.busy():
            QMessageBox.information(self, APP_NAME, "Please wait – a job is still running.")
            e.ignore()
            return
        save_config(self.cfg)
        e.accept()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("StellaSora.ModManager")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    APP_HOME.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(APP_HOME / ".instance.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "The mod manager is already running.\n"
                                "Look for its window or its icon on the taskbar.")
        sys.exit(0)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
