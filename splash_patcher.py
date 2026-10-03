"""
DaVinci Resolve Splash Patcher
==============================

Replaces the startup splash screens of DaVinci Resolve (Windows and Linux) with your own images.

How it works
------------
The splash images are not loose files: they are compiled into Resolve.exe as Qt
resources. There are two independent resource sets:
  * 1x  ":/Application/Misc/ResolveSplashScreenN"      1110x490  (+16 unused _Linux variants)
  * 2x  ":/Application/Misc/ResolveSplashScreenN@2x"   2220x980  (used when Windows scaling > 100%)
A Qt resource consists of three tables: data (each entry = 4-byte big-endian size + raw
bytes), names (UTF-16BE) and a tree whose file nodes store an offset into the data table.

Instead of squeezing a new PNG into the exact byte budget of the original (the manual
hex-editor approach), the patcher:
  1. locates the tables by signature (no hard-coded offsets -> survives updates),
  2. treats the data of every splash entry as free space, merging neighbouring entries
     into large contiguous regions,
  3. packs the newly rendered PNGs into that space and rewrites the tree offsets.
So images stay lossless in almost every case; palette quantisation is only a fallback.

Before the first patch the original bytes are backed up to %APPDATA%\\ResolveSplashPatcher,
so "Restore" works and re-patching always starts from the pristine layout.

Usage
-----
  pythonw splash_patcher.py            interface (Windows: Edge app window)
  python3 splash_patcher.py            interface (Linux: Chromium/Chrome/Edge app window, else browser tab)
  python  splash_patcher.py --apply    apply the saved configuration
  python  splash_patcher.py --auto     like --apply, but only if Resolve is not patched yet
                                       (used by the scheduled task / systemd unit after updates)
  python  splash_patcher.py --restore  restore the original splash screens
  python  splash_patcher.py --check    (read-only) show what was found in the Resolve binary
  python  splash_patcher.py --find     (Linux) search /opt/resolve for the file holding the splash screens

On Linux the target is the `resolve` ELF binary (default /opt/resolve/bin/resolve) and the
settings live in ~/.config/ResolveSplashPatcher. Root is requested through pkexec/sudo only
when the binary is not writable by the current user.
"""

import ctypes
import functools
import io
import json
import mmap
import os
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zlib
from dataclasses import dataclass, field

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import ctypes.wintypes as wt

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    _msg = ("Pillow is not installed. Install it with:\n\npython -m pip install pillow\n\n"
            "Не найден модуль Pillow. Установите его командой выше.")
    if IS_WIN:
        ctypes.windll.user32.MessageBoxW(None, _msg, "Resolve Splash Patcher", 0x10)
    else:
        print(_msg, file=sys.stderr)
    sys.exit(1)

APP_NAME = "ResolveSplashPatcher"


def _default_app_dir():
    """Windows: %APPDATA%\\ResolveSplashPatcher. Linux: ~/.config/ResolveSplashPatcher, also when
    the script runs as root through pkexec/sudo (then the invoking user's home is used)."""
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    home = None
    if os.geteuid() == 0:
        uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
        if uid:
            try:
                import pwd
                home = pwd.getpwuid(int(uid)).pw_dir
            except (KeyError, ValueError, ImportError):
                home = None
    if home:
        return os.path.join(home, ".config", APP_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def _set_app_dir(path):
    global APP_DIR, CONFIG_PATH, IMAGES_DIR, BACKUP_DIR, LOG_PATH
    APP_DIR = os.path.abspath(path)
    CONFIG_PATH = os.path.join(APP_DIR, "config.json")
    IMAGES_DIR = os.path.join(APP_DIR, "images")
    BACKUP_DIR = os.path.join(APP_DIR, "backups")
    LOG_PATH = os.path.join(APP_DIR, "patcher.log")


_set_app_dir(_default_app_dir())
if IS_WIN:
    DEFAULT_EXE = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
    TARGET_NAME = "Resolve.exe"
else:
    DEFAULT_EXE = "/opt/resolve/bin/resolve"
    TARGET_NAME = "resolve"
    # usual install locations of DaVinci Resolve on Linux (the installer defaults to /opt/resolve)
    RESOLVE_DIRS = ("/opt/resolve", "/opt/resolve-studio", "/opt/DaVinciResolve",
                    os.path.expanduser("~/resolve"), os.path.expanduser("~/DaVinciResolve"))
TASK_NAME = "ResolveSplashPatcher"
SERVICE_NAME = "resolve-splash-patcher"
HERE = os.path.dirname(os.path.abspath(__file__))

PNG_SIG = b"\x89PNG\r\n\x1a\n"
IMAGE_SIGS = (PNG_SIG, b"\xff\xd8\xff", b"GIF8", b"BM")
MARKER = b"RSPATCH1"
# resource set id -> name of its first splash entry (used as anchor to find the tables)
GROUPS = (("1x", ("ResolveSplashScreen1", "ResolveSplashScreen_Linux1")),
          ("2x", ("ResolveSplashScreen1@2x", "ResolveSplashScreen_Linux1@2x")))
MIRROR = "<mirror>"      # marker: a *_Linux node that simply shares the data of its Windows twin
WIN_SLOT_RE = re.compile(r"^ResolveSplashScreen(\d+)(?:@2x)?$")
LINUX_SLOT_RE = re.compile(r"^ResolveSplashScreen_Linux(\d+)(?:@2x)?$")
# Colour and shape of the dark panel on the left of the stock splash screens (measured on
# the original images: a near-solid navy panel up to ~30% of the width, faded out by ~62%).
DARK_RGB = (24, 29, 37)
DARK_ALPHA = 0.94
DARK_SOLID, DARK_END = 0.30, 0.62

CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0


def log(msg):
    os.makedirs(APP_DIR, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        try:
            print(line)
        except Exception:
            pass


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Localisation of messages shown to the user (the interface has its own string table)
# --------------------------------------------------------------------------------------

LANG = "en"
MESSAGES = {
    "zstd": ("zstd-compressed resources are not supported", "zstd-сжатые ресурсы не поддерживаются"),
    "no_data_table": ("Could not find the resource data table", "Не удалось найти таблицу данных ресурсов"),
    "no_resources": ("No splash screen resources found in Resolve.exe. The format may have changed in this "
                     "Resolve version.", "В Resolve.exe не найдены ресурсы заставки. Возможно, формат изменился "
                     "в новой версии Resolve."),
    "close_for_restore": ("Close DaVinci Resolve before restoring.", "Закройте DaVinci Resolve перед восстановлением."),
    "no_backup": ("No backup found for this Resolve version. Repair Resolve with its installer.",
                  "Резервная копия для этой версии не найдена. Восстановите Resolve через установщик (Repair)."),
    "restoring": ("Restoring the original splash screens…", "Восстановление оригинальных заставок…"),
    "restore_failed": ("Restore failed: the patch marker is still present",
                       "Восстановление не удалось: метка патча всё ещё на месте"),
    "restored": ("Original splash screens restored", "Оригинальные заставки восстановлены"),
    "bad_image": ("Could not open image “{name}”", "Не удалось открыть изображение «{name}»"),
    "no_marker_space": ("No room left for the patch marker", "Нет места для служебной метки"),
    "not_found": ("File not found: {path}", "Файл не найден: {path}"),
    "resolve_running": ("DaVinci Resolve is running. Close it and try again.",
                        "DaVinci Resolve запущен. Закройте его и попробуйте снова."),
    "no_images": ("No images selected", "Не выбрано ни одного изображения"),
    "analyzing": ("Reading Resolve.exe…", "Анализ Resolve.exe…"),
    "patched_no_backup": ("Resolve.exe is already patched but the backup is missing. "
                          "Repair Resolve with its installer.",
                          "Resolve.exe уже пропатчен, но резервная копия не найдена. "
                          "Восстановите Resolve через установщик (Repair)."),
    "rollback": ("Returning to the original layout…", "Возврат к исходной раскладке…"),
    "rollback_failed": ("Could not return to the original resource layout",
                        "Не удалось вернуть исходную раскладку ресурсов"),
    "all_original": ("Every slot is set to Original, nothing to apply",
                     "Все слоты оставлены оригинальными, применять нечего"),
    "preparing": ("Preparing {name} ({group})", "Подготовка {name} ({group})"),
    "no_space": ("The images don't fit even after compression. Use fewer images.",
                 "Изображения не помещаются даже после сжатия. Выберите меньше картинок."),
    "quantizing": ("Low on space, reducing colours: {name}", "Мало места, сжимаю палитрой: {name}"),
    "writing": ("Writing Resolve.exe…", "Запись в Resolve.exe…"),
    "verifying": ("Verifying…", "Проверка…"),
    "verify_marker": ("Verification failed: no marker in set {group}",
                      "Проверка не пройдена: нет метки в наборе {group}"),
    "verify_slot": ("Verification failed for slot {num} ({group})", "Проверка не пройдена для слота {num} ({group})"),
    "done": ("Done", "Готово"),
    "task_failed": ("Could not create the scheduled task: {err}", "Не удалось создать задачу: {err}"),
    "uac_denied": ("Administrator permission was declined", "Запрос прав администратора отклонён"),
    "uac_failed": ("Could not start the process as administrator",
                   "Не удалось запустить процесс с правами администратора"),
    "exe_missing": ("Resolve.exe not found. Choose its location.", "Resolve.exe не найден. Укажите путь к нему."),
    "busy": ("Another operation is already running", "Уже выполняется другая операция"),
    "waiting_admin": ("Waiting for administrator permission…", "Ожидание прав администратора…"),
    "op_failed": ("The operation failed (see patcher.log)", "Операция завершилась с ошибкой (см. patcher.log)"),
    "pick_exe": ("Choose Resolve.exe", "Выберите Resolve.exe"),
    "no_dialog": ("No file dialog available. Install zenity (or python3-tk), or set \"exe\" in config.json.",
                  "Нет диалога выбора файла. Установите zenity (или python3-tk) либо укажите \"exe\" в config.json."),
    "no_elevation": ("Root permission is needed, but neither pkexec (graphical session) nor an interactive sudo "
                     "is available. Run this in a terminal instead:\n{cmd}",
                     "Нужны права root, но pkexec (графическая сессия) и интерактивный sudo недоступны. "
                     "Выполните в терминале:\n{cmd}"),
}
# Linux wording (everything else only has \"Resolve.exe\" swapped for the file name of the target)
LINUX_MESSAGES = {
    "waiting_admin": ("Waiting for root permission…", "Ожидание прав root…"),
    "uac_denied": ("Root permission was declined", "Запрос прав root отклонён"),
    "uac_failed": ("Could not start the process as root", "Не удалось запустить процесс с правами root"),
}


def tr(key, **kw):
    en, ru = MESSAGES[key]
    if not IS_WIN:
        en, ru = LINUX_MESSAGES.get(key, (en, ru))
        en, ru = en.replace("Resolve.exe", TARGET_NAME), ru.replace("Resolve.exe", TARGET_NAME)
    return (ru if LANG == "ru" else en).format(**kw)


def set_lang(lang):
    global LANG
    LANG = "ru" if lang == "ru" else "en"


# --------------------------------------------------------------------------------------
# Qt resource parsing
# --------------------------------------------------------------------------------------

def qt_hash(name):
    h = 0
    for ch in name:
        h = ((h << 4) + ord(ch)) & 0xFFFFFFFF
        h ^= (h & 0xF0000000) >> 23
        h &= 0x0FFFFFFF
    return h


def read_name_entry(buf, pos):
    """Returns (name, entry_length) if a valid Qt name entry starts at pos, else None."""
    if pos < 0 or pos + 6 > len(buf):
        return None
    length, h = struct.unpack(">HI", buf[pos:pos + 6])
    if length == 0 or length > 512 or pos + 6 + 2 * length > len(buf):
        return None
    try:
        name = buf[pos + 6:pos + 6 + 2 * length].decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    if qt_hash(name) != h:
        return None
    return name, 6 + 2 * length


@dataclass
class Node:
    index: int
    pos: int          # absolute file offset of the tree node
    name: str
    flags: int
    data_off: int     # offset relative to the data table (file nodes only)


@dataclass
class Layout:
    group: str
    tree: int
    names: int
    data: int
    node_size: int
    node_count: int
    files: list = field(default_factory=list)
    data_end: int = 0         # end of the data table (names offset when the data precedes the names)
    scan_end: int = 0         # where to look for our marker (the table may extend past the last entry)

    def entry_start(self, node):
        return self.data + node.data_off

    def entry_size(self, buf, node):
        s = self.entry_start(node)
        return 4 + struct.unpack(">I", buf[s:s + 4])[0]

    def read(self, buf, node):
        start = self.entry_start(node)
        size = struct.unpack(">I", buf[start:start + 4])[0]
        raw = bytes(buf[start + 4:start + 4 + size])
        if node.flags & 1:  # zlib: 4-byte uncompressed size + zlib stream
            return zlib.decompress(raw[4:])
        if node.flags & 4:
            raise PatchError(tr("zstd"))
        return raw

    def _slots(self, rx):
        out = {}
        for n in self.files:
            m = rx.match(n.name)
            if m:
                out[int(m.group(1))] = n
        return dict(sorted(out.items()))

    def win_slots(self):
        # a build that only ships the *_Linux names uses those as its primary slots
        return self._slots(WIN_SLOT_RE) or self._slots(LINUX_SLOT_RE)

    def linux_slots(self):
        return self._slots(LINUX_SLOT_RE) if self._slots(WIN_SLOT_RE) else {}

    def tree_range(self):
        return self.tree, self.tree + self.node_size * self.node_count


def _walk_tree(buf, tree, node_size, limit=3000000):
    """Walk a Qt resource tree; returns list of (index, pos, name_off, flags, data_off|None).
    Raises ValueError(reason) when the bytes at `tree` are not a plausible tree."""
    def raw(i):
        p = tree + node_size * i
        if p + node_size > len(buf):
            raise ValueError(f"node {i} lies beyond the end of the file")
        name_off, flags = struct.unpack(">IH", buf[p:p + 6])
        if flags & 2:
            count, first = struct.unpack(">II", buf[p + 6:p + 14])
            return p, name_off, flags, (count, first)
        _country, _lang, data_off = struct.unpack(">HHI", buf[p + 6:p + 14])
        return p, name_off, flags, data_off

    out, stack, seen = [], [0], set()
    while stack:
        i = stack.pop()
        if i in seen or i > limit:
            raise ValueError(f"node {i}: repeated or beyond limit")
        seen.add(i)
        p, name_off, flags, extra = raw(i)
        if flags & ~0x7:
            raise ValueError(f"node {i}: unknown flags 0x{flags:x}")
        if flags & 2:
            count, first = extra
            if count == 0 or count > 1000000 or first <= i or first + count > limit:
                raise ValueError(f"node {i}: bad directory (count={count}, first={first})")
            stack.extend(range(first, first + count))
            out.append((i, p, name_off, flags, None))
        else:
            out.append((i, p, name_off, flags, extra))
    return out


def _validate_data(buf, data, names, by_off):
    for i, n in enumerate(by_off):
        start = data + n.data_off
        if start + 8 > names:
            return False
        size = struct.unpack(">I", buf[start:start + 4])[0]
        limit = data + by_off[i + 1].data_off if i + 1 < len(by_off) else names
        if start + 4 + size > limit:
            return False
        head = bytes(buf[start + 4:start + 12])
        if n.flags & 1:
            if size < 6 or buf[start + 8] != 0x78:
                return False
        elif not head.startswith(IMAGE_SIGS):
            if WIN_SLOT_RE.match(n.name) or LINUX_SLOT_RE.match(n.name):
                return False
    return True


def _find_data_table(buf, names, files):
    """The last image signature before the names table belongs to some entry k, so
    data = pos - 4 - k.data_off; the candidate is validated against the whole table."""
    lo = max(0, names - 256 * 1024 * 1024)
    by_off = sorted({n.data_off: n for n in files}.values(), key=lambda n: n.data_off)
    for sig in (PNG_SIG, b"\xff\xd8\xff"):
        p = buf.rfind(sig, lo, names)
        if p < 0:
            continue
        for cand in sorted(files, key=lambda n: -n.data_off):
            data = p - 4 - cand.data_off
            if data >= 0 and _validate_data(buf, data, names, by_off):
                return data
    raise PatchError(tr("no_data_table"))


_ROOT_RX = re.compile(rb"\x00\x00\x00\x00\x00\x02[\x00-\xff]{4}\x00\x00\x00\x01")
MIN_SLOTS = 4          # a real splash resource lists at least this many slot names
ADJACENT = 512         # max gap (alignment padding) between neighbouring tables


def _tree_candidates(buf, names_end, entry):
    """Offsets where a resource tree may start (root node: name 0, directory, first child 1).
    Windows builds store data, names, tree (tree right after the names); the Linux build stores
    tree, names, data (tree right before the names)."""
    seen = set()
    for tree in range(names_end, min(names_end + ADJACENT, len(buf) - 14)):
        if struct.unpack(">IH", buf[tree:tree + 6]) == (0, 2) and \
                struct.unpack(">I", buf[tree + 10:tree + 14])[0] == 1:
            seen.add(tree)
            yield tree
    lo = max(0, entry - 32 * 1024 * 1024)
    hits = [m.start() for m in _ROOT_RX.finditer(buf, lo, entry)]
    for tree in reversed(hits):          # closest to the names first
        if tree not in seen:
            yield tree


def _resolve_names(buf, names, nodes):
    """Reads the name of every node. All of them must be readable (a few misses are allowed only
    in big trees). Returns [Node] or None."""
    named, bad = [], 0
    for (i, pos, name_off, flags, data_off) in nodes:
        if i == 0:
            continue
        r = read_name_entry(buf, names + name_off)
        if not r:
            bad += 1
            continue
        named.append(Node(i, pos, r[0], flags, data_off))
    if bad > len(nodes) // 50:
        return None
    return named


def _validate_data_after(buf, data, files):
    """Data table that follows the names: every entry must fit before the next one and the
    splash entries must hold images. Returns the end of the table or None."""
    by_off = sorted({n.data_off: n for n in files}.values(), key=lambda n: n.data_off)
    end = 0
    for i, n in enumerate(by_off):
        start = data + n.data_off
        if start + 8 > len(buf):
            return None
        size = struct.unpack(">I", buf[start:start + 4])[0]
        limit = data + by_off[i + 1].data_off if i + 1 < len(by_off) else len(buf)
        if start + 4 + size > limit:
            return None
        if not n.flags & 5 and not bytes(buf[start + 4:start + 12]).startswith(IMAGE_SIGS):
            if WIN_SLOT_RE.match(n.name) or LINUX_SLOT_RE.match(n.name):
                return None
        end = max(end, start + 4 + size)
    return end


def _find_data_after(buf, names_end, files):
    for data in range(names_end, min(names_end + 256, len(buf))):
        end = _validate_data_after(buf, data, files)
        if end:
            return data, end
    return None


def locate(buf, group, anchor_name, trace=lambda msg: None):
    """Locate the Qt resource containing `anchor_name`. Works on original and on
    previously patched executables (name and tree tables are never relocated)."""
    anchor = anchor_name.encode("utf-16-be")
    search = 0
    while True:
        p = buf.find(anchor, search)
        if p < 0:
            return None
        search = p + 1
        entry = p - 6
        r = read_name_entry(buf, entry)
        if not r or r[0] != anchor_name:
            continue
        q = entry
        while True:
            r = read_name_entry(buf, q)
            if not r:
                break
            q += r[1]
        names_end = q
        trace(f"anchor {anchor_name} at 0x{entry:x}, name entries end at 0x{names_end:x}")
        for tree in _tree_candidates(buf, names_end, entry):
            for node_size in (22, 14):
                try:
                    nodes = _walk_tree(buf, tree, node_size)
                except (ValueError, struct.error) as e:
                    trace(f"  tree 0x{tree:x} size {node_size}: {e}")
                    continue
                tree_end = tree + node_size * (max(n[0] for n in nodes) + 1)
                for cand in (n for n in nodes if n[4] is not None):
                    names = entry - cand[2]
                    if names < 0 or not read_name_entry(buf, names):
                        continue
                    tree_first = tree_end <= names
                    gap = names - tree_end if tree_first else tree - names_end
                    if not 0 <= gap < ADJACENT:
                        continue
                    named = _resolve_names(buf, names, nodes)
                    if not named or not any(n.name == anchor_name for n in named):
                        continue
                    files = [n for n in named if n.data_off is not None]
                    nslots = sum(1 for n in files
                                 if WIN_SLOT_RE.match(n.name) or LINUX_SLOT_RE.match(n.name))
                    if nslots < MIN_SLOTS:
                        trace(f"  tree 0x{tree:x} size {node_size}: only {nslots} slot names")
                        continue
                    if tree_first:
                        found = _find_data_after(buf, names_end, files)
                        if not found:
                            trace(f"  tree 0x{tree:x}: no data table after the names")
                            continue
                        data, data_end = found
                    else:
                        data = _find_data_table(buf, names, files)
                        data_end = names
                    trace(f"  OK: tree 0x{tree:x} (size {node_size}, {len(nodes)} nodes), names "
                          f"0x{names:x}, data 0x{data:x}, order "
                          f"{'tree-names-data' if tree_first else 'data-names-tree'}")
                    return Layout(group=group, tree=tree, names=names, data=data,
                                  node_size=node_size, node_count=max(n[0] for n in nodes) + 1,
                                  files=files, data_end=data_end,
                                  scan_end=min(len(buf), data_end + (64 << 20)) if tree_first else 0)


def diagnose(path):
    """Read-only: explains step by step what the locator sees in the binary."""
    print(f"File: {path}")
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        print(f"Size: {len(mm) / 1e6:.1f} MB")
        for gid, anchors in GROUPS:
            for anchor in anchors:
                n = mm.count(anchor.encode("utf-16-be")) if hasattr(mm, "count") else "?"
                print(f"\n[{gid}] {anchor}")
                lay = locate(mm, gid, anchor, trace=lambda m: print("   ", m))
                if lay:
                    print(f"  -> found: {len(lay.win_slots())} primary / {len(lay.linux_slots())} "
                          f"Linux slots, {len(lay.files)} files")
        try:
            lays = locate_all(mm)
            print("\nlocate_all: OK")
            for g, lay in lays.items():
                node = next(iter(lay.win_slots().values()))
                try:
                    size = Image.open(io.BytesIO(lay.read(mm, node))).size
                except Exception as e:      # noqa: BLE001
                    size = f"unreadable ({e})"
                print(f"  {g}: first slot {node.name} -> {size}")
        except PatchError as e:
            print(f"\nlocate_all: {e}")


def _plausible_splash(buf, lay):
    """Safety net: every primary slot must decode to a wide banner image. Never write into a table
    that merely looks like a resource tree."""
    slots = lay.win_slots()
    if not slots:
        return False
    for node in slots.values():
        try:
            w, h = Image.open(io.BytesIO(lay.read(buf, node))).size
        except Exception:      # noqa: BLE001
            return False
        if w < 600 or h < 250 or not 1.5 <= w / h <= 4:
            return False
    return True


def locate_all(buf):
    out = {}
    for gid, anchors in GROUPS:
        for anchor in anchors:
            lay = locate(buf, gid, anchor)
            if lay and _plausible_splash(buf, lay):
                out[gid] = lay
                break
    if not out:
        raise PatchError(tr("no_resources"))
    return out


# --------------------------------------------------------------------------------------
# Executable helpers
# --------------------------------------------------------------------------------------

def file_version(path):
    if not IS_WIN:
        return "unknown"        # ELF files carry no version resource; see exe_identity()
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return "unknown"
        buf = ctypes.create_string_buffer(size)
        ver.GetFileVersionInfoW(path, 0, size, buf)
        p, n = ctypes.c_void_p(), ctypes.c_uint()
        ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n))
        ffi = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms, ls = ffi[2], ffi[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return "unknown"


def backup_key(exe_path, version=None):
    return f"{version or file_version(exe_path)}_{os.path.getsize(exe_path)}"


def exe_identity(path, mm, layouts, version=None):
    """Returns (version, backup key). The key must be the same before and after patching.
    Windows: file version + size. Linux has no version resource, so the key is the size plus a
    hash of samples from the part of the file in front of the resource tables (never patched)."""
    size = os.path.getsize(path)
    version = version or file_version(path)
    if IS_WIN:
        return version, backup_key(path, version)
    import hashlib
    h = hashlib.sha1()
    # Only bytes in front of every resource table are hashed: the patcher never touches them, so the
    # key is the same before and after patching. 32 samples spread over that part of the file.
    limit = min(min(lay.data, lay.tree, lay.names) for lay in layouts.values())
    chunk = 32 * 1024
    if limit <= 32 * chunk:
        h.update(mm[:limit])
    else:
        step = (limit - chunk) // 31
        for i in range(32):
            h.update(mm[i * step:i * step + chunk])
    digest = h.hexdigest()
    return f"linux-{digest[:8]}", f"linux_{digest[:16]}_{size}"


def backup_dir(key, group):
    # the 1x set lives in the root of the version folder (compatible with older backups)
    d = os.path.join(BACKUP_DIR, key)
    return d if group == "1x" else os.path.join(d, group)


def resolve_running():
    if not IS_WIN:
        try:
            pids = [d for d in os.listdir("/proc") if d.isdigit()]
        except OSError:
            return False
        for pid in pids:
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                    if f.read().strip().lower() == "resolve":
                        return True
            except OSError:
                continue
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
                             capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout
        return "Resolve.exe" in out
    except Exception:
        return False


def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def needs_elevation(exe):
    """Linux: root is only requested when the target file is not writable for us."""
    return not IS_WIN and os.geteuid() != 0 and os.path.exists(exe) and not os.access(exe, os.W_OK)


_SPLASH_NEEDLE = "ResolveSplashScreen1".encode("utf-16-be")


def has_splash_resources(path):
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"\x7fELF" and not IS_WIN:
                return False
            if os.fstat(f.fileno()).st_size < 4096:
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm.find(_SPLASH_NEEDLE) >= 0
    except (OSError, ValueError):
        return False


def find_resolve_binary():
    """Linux: finds the file that embeds the splash screens. Normally bin/resolve; if that one
    has none (the Qt resources can live in a shared library), every ELF file below the install
    directory is searched."""
    if IS_WIN:
        return None
    roots = [d for d in RESOLVE_DIRS if os.path.isdir(d)]
    for r in roots:
        main = os.path.join(r, "bin", "resolve")
        if os.path.isfile(main) and has_splash_resources(main):
            return main
    for r in roots:
        for dp, _dn, files in os.walk(r):
            for name in sorted(files):
                p = os.path.join(dp, name)
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                if has_splash_resources(p):
                    return p
    return None


@functools.lru_cache(maxsize=1)
def guess_exe():
    if IS_WIN:
        return DEFAULT_EXE
    if os.path.isfile(DEFAULT_EXE) and has_splash_resources(DEFAULT_EXE):
        return DEFAULT_EXE
    return find_resolve_binary() or DEFAULT_EXE


def find_marker(buf, layout):
    end = layout.scan_end or layout.data_end
    p = buf.find(MARKER, layout.data, end)
    while p >= 0:
        length = struct.unpack(">I", buf[p + 8:p + 12])[0]
        try:
            meta = json.loads(bytes(buf[p + 12:p + 12 + length]).decode("utf-8"))
        except Exception:
            meta = {}
        if not layout.scan_end or meta.get("group") == layout.group:
            return meta
        p = buf.find(MARKER, p + 1, end)      # a marker of another set: keep looking
    return None


@dataclass
class ExeInfo:
    path: str
    version: str
    key: str
    layouts: dict            # group -> Layout
    markers: dict            # group -> marker JSON or None

    @property
    def state(self):
        n = sum(1 for m in self.markers.values() if m is not None)
        return "original" if n == 0 else ("patched" if n == len(self.markers) else "partial")

    def backup_ok(self, group):
        return os.path.isfile(os.path.join(backup_dir(self.key, group), "meta.json"))

    @property
    def patched_time(self):
        return next((m.get("time") for m in self.markers.values() if m), None)


def inspect_exe(path):
    version = file_version(path)
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        markers = {g: find_marker(mm, lay) for g, lay in layouts.items()}
        version, key = exe_identity(path, mm, layouts, version)
    return ExeInfo(path, version, key, layouts, markers)


def print_check(path):
    """Read-only diagnostics: what the patcher finds in the binary."""
    info = inspect_exe(path)
    print(f"File:    {path}")
    print(f"Version: {info.version}   state: {info.state}   backup key: {info.key}")
    for g, lay in info.layouts.items():
        win, lin = lay.win_slots(), lay.linux_slots()
        sizes = set()
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for n in list(win.values())[:1] + list(lin.values())[:1]:
                try:
                    sizes.add((n.name, Image.open(io.BytesIO(lay.read(mm, n))).size))
                except Exception as e:      # noqa: BLE001
                    sizes.add((n.name, f"unreadable: {e}"))
            regions = _free_regions(mm, lay, [])
        print(f"  set {g}: {len(win)} primary slots, {len(lin)} *_Linux slots, "
              f"free space {sum(e - s for s, e in regions) / 1e6:.1f} MB, "
              f"patched: {info.markers[g] is not None}")
        for name, size in sorted(sizes):
            print(f"    {name}: {size}")
    return {"checked": True}


# --------------------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------------------

def _splash_spans(buf, layout):
    nodes = list(layout.win_slots().values()) + list(layout.linux_slots().values())
    return [(layout.entry_start(n), layout.entry_start(n) + layout.entry_size(buf, n)) for n in nodes]


def create_backup(buf, layout, key, version, size):
    d = backup_dir(key, layout.group)
    os.makedirs(d, exist_ok=True)
    t0, t1 = layout.tree_range()
    spans = _splash_spans(buf, layout)
    d0, d1 = min(s for s, _ in spans), max(e for _, e in spans)
    with open(os.path.join(d, "tree.bin"), "wb") as f:
        f.write(buf[t0:t1])
    with open(os.path.join(d, "data.bin"), "wb") as f:
        f.write(buf[d0:d1])
    for num, node in layout.win_slots().items():
        with open(os.path.join(d, f"orig_{num:02d}.png"), "wb") as f:
            f.write(layout.read(buf, node))
    meta = {"version": version, "size": size, "group": layout.group,
            "tree": [t0, t1], "data": [d0, d1], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    log(f"Backup created: {d}")


def load_backup(key, group, size):
    d = backup_dir(key, group)
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(d, "tree.bin"), "rb") as f:
            tree = f.read()
        with open(os.path.join(d, "data.bin"), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if meta["size"] != size:
        return None
    return meta, tree, data


def write_backup_into(fh, backup):
    meta, tree, data = backup
    fh.seek(meta["tree"][0])
    fh.write(tree)
    fh.seek(meta["data"][0])
    fh.write(data)


def original_slot_pngs(info):
    """{slot: png bytes} of the original 1x splash screens (from backup if patched)."""
    group = "1x" if "1x" in info.layouts else next(iter(info.layouts))
    layout = info.layouts[group]
    out = {}
    if info.markers[group] is not None:
        d = backup_dir(info.key, group)
        for num in layout.win_slots():
            p = os.path.join(d, f"orig_{num:02d}.png")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    out[num] = f.read()
        return out
    with open(info.path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for num, node in layout.win_slots().items():
            out[num] = layout.read(mm, node)
    return out


def restore_original(exe_path, progress=lambda m, f=None: None):
    if resolve_running():
        raise PatchError(tr("close_for_restore"))
    info = inspect_exe(exe_path)
    if info.state == "original":
        log("Resolve.exe is not patched – nothing to restore")
        return {"restored": 0}
    size = os.path.getsize(exe_path)
    backups = {}
    for g, m in info.markers.items():
        if m is not None:
            backups[g] = load_backup(info.key, g, size)
            if not backups[g]:
                raise PatchError(tr("no_backup"))
    progress(tr("restoring"), 0.4)
    with open(exe_path, "r+b") as fh:
        for b in backups.values():
            write_backup_into(fh, b)
    if inspect_exe(exe_path).state != "original":
        raise PatchError(tr("restore_failed"))
    log("Original splash screens restored")
    progress(tr("restored"), 1.0)
    return {"restored": len(backups)}


# --------------------------------------------------------------------------------------
# Rendering (the interface mirrors this math in JavaScript for the live preview)
# --------------------------------------------------------------------------------------

def load_source(path, max_side=5000):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def template_geometry(template):
    """Bounding box of the opaque photo area inside the original splash (shadow excluded)."""
    a = template.getchannel("A")
    box = a.point(lambda v: 255 if v == 255 else 0).getbbox()
    return box or (0, 0) + template.size


def crop_box(sw, sh, w, h, fx, fy, zoom):
    aspect = w / h
    if sw / sh > aspect:
        ch, cw = sh, sh * aspect
    else:
        cw, ch = sw, sw / aspect
    zoom = max(1.0, zoom)
    cw, ch = cw / zoom, ch / zoom
    left, top = (sw - cw) * fx, (sh - ch) * fy
    return left, top, left + cw, top + ch


def darken_alpha(t):
    """Opacity of the left panel at relative x position t (0..1). Mirrored in ui.html."""
    if t <= DARK_SOLID:
        return DARK_ALPHA
    if t >= DARK_END:
        return 0.0
    u = (t - DARK_SOLID) / (DARK_END - DARK_SOLID)
    return DARK_ALPHA * (1 - u * u * (3 - 2 * u))   # smoothstep fade


def render_splash(src, template, fx=0.5, fy=0.5, zoom=1.0, darken=True):
    """src: RGB image. template: original RGBA splash (gives size, shadow and rounded corners)."""
    template = template.convert("RGBA")
    x0, y0, x1, y1 = template_geometry(template)
    w, h = x1 - x0, y1 - y0
    photo = src.resize((w, h), Image.LANCZOS, box=crop_box(*src.size, w, h, fx, fy, zoom))
    if darken:
        grad = Image.new("L", (w, 1))
        grad.putdata([round(255 * darken_alpha((x + 0.5) / w)) for x in range(w)])
        grad = grad.resize((w, h))
        photo = Image.composite(Image.new("RGB", (w, h), DARK_RGB), photo, grad)
    out = template.copy()
    out.paste(photo, (x0, y0))
    out.putalpha(template.getchannel("A"))
    return out


def encode_png(img, quantize=False):
    if quantize:
        img = img.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
    bio = io.BytesIO()
    img.save(bio, "PNG", compress_level=7)
    return bio.getvalue()


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

def default_config():
    return {"exe": guess_exe(), "images": [], "slots": {}, "darken": True, "lang": "en"}


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        base = default_config()
        base.update(cfg)
        set_lang(base.get("lang"))
        return base
    except (OSError, ValueError):
        return default_config()


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def import_image_bytes(data, filename):
    """Stores a user image in the app folder so the auto-patcher still has it later."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    img_id = uuid.uuid4().hex[:10]
    ext = os.path.splitext(filename)[1].lower() or ".png"
    dst = os.path.join(IMAGES_DIR, img_id + ext)
    with open(dst, "wb") as f:
        f.write(data)
    try:
        with Image.open(dst) as im:
            im.verify()
    except Exception:
        os.remove(dst)
        raise PatchError(tr("bad_image", name=filename))
    return {"id": img_id, "file": dst, "name": filename, "fx": 0.5, "fy": 0.5, "zoom": 1.0}


def import_image(path):
    with open(path, "rb") as f:
        return import_image_bytes(f.read(), os.path.basename(path))


def effective_slots(cfg, slot_numbers):
    """slot -> image id (None = keep original). 'auto' slots are filled round-robin."""
    ids = [im["id"] for im in cfg["images"]]
    out = {}
    for i, num in enumerate(slot_numbers):
        v = cfg["slots"].get(str(num), "auto")
        if v == "auto":
            out[num] = ids[i % len(ids)] if ids else None
        elif v in ids:
            out[num] = v
        else:
            out[num] = None
    return out


# --------------------------------------------------------------------------------------
# Patching
# --------------------------------------------------------------------------------------

def _free_regions(buf, layout, keep_nodes):
    """Contiguous runs of splash entries that may be overwritten."""
    keep_offs = {n.data_off for n in keep_nodes}
    spans = sorted({(layout.entry_start(n), layout.entry_start(n) + layout.entry_size(buf, n), n.name)
                    for n in layout.files})
    regions, cur = [], None
    for start, end, name in spans:
        free = (WIN_SLOT_RE.match(name) or LINUX_SLOT_RE.match(name)) and \
               (start - layout.data) not in keep_offs
        if free:
            if cur and start <= cur[1] + 16:
                cur[1] = max(cur[1], end)
            else:
                cur = [start, end]
                regions.append(cur)
        else:
            cur = None
    return [tuple(r) for r in regions]


def _pack(blobs, regions):
    """First-fit decreasing. blobs: {id: bytes}. Returns {id: abs_pos} or None."""
    free = [[s, e] for s, e in regions]
    placed = {}
    for bid, blob in sorted(blobs.items(), key=lambda kv: -len(kv[1])):
        need = 4 + len(blob)
        for r in sorted(free, key=lambda r: r[1] - r[0]):
            if r[1] - r[0] >= need:
                placed[bid] = r[0]
                r[0] += need
                break
        else:
            return None
    return placed


def _plan_linux(mm, layout, template, assignment, keep):
    """Decides what the *_Linux nodes of a set point to. Returns (template, {slot: value}) where the
    value is a blob key, MIRROR (share the Windows twin) or None (keep own original data; those
    nodes are appended to `keep`). Windows keeps the old behaviour (Linux variants are never
    shown there). On Linux the variants are the ones Resolve shows, so they get the user's images
    too, rendered on their own template when its size/shape differs."""
    win, lin = layout.win_slots(), layout.linux_slots()
    first_used = next((i for i in assignment.values() if i is not None), None)
    lin_tpl, separate = template, False
    if lin and not IS_WIN:
        try:
            cand = Image.open(io.BytesIO(layout.read(mm, next(iter(lin.values()))))).convert("RGBA")
            if cand.size != template.size or template_geometry(cand) != template_geometry(template):
                lin_tpl, separate = cand, True
        except Exception:      # noqa: BLE001 - unreadable original: fall back to the Windows template
            pass
    out = {}
    for num, node in lin.items():
        img = assignment.get(num, first_used)
        if img is None:
            if IS_WIN:
                out[num] = MIRROR if num in win else None
            else:
                out[num] = None
                keep.append(node)
        else:
            out[num] = img + ":L" if separate else img
    return lin_tpl, out


def _blob_templates(p):
    """blob key -> template it is rendered on ('<image id>' or '<image id>:L' for Linux variants)."""
    need = {}
    for v in p["assignment"].values():
        if v is not None:
            need[v] = p["template"]
    for v in p["lin_assign"].values():
        if v is not None and v != MIRROR:
            need[v] = p["lin_template"] if v.endswith(":L") else p["template"]
    return need


def _write_group(fh, layout, assignment, lin_assign, regions, blobs, placed, meta):
    win, lin = layout.win_slots(), layout.linux_slots()
    for s, e in regions:                      # no stale PNGs left behind
        fh.seek(s)
        fh.write(b"\0" * (e - s))
    for img_id, pos in placed.items():
        fh.seek(pos)
        fh.write(struct.pack(">I", len(blobs[img_id])) + blobs[img_id])

    def point(node, flags, data_off):
        fh.seek(node.pos + 4)
        fh.write(struct.pack(">H", flags))
        fh.seek(node.pos + 10)
        fh.write(struct.pack(">I", data_off))

    for num, node in win.items():
        if assignment[num] is not None:
            point(node, 0, placed[assignment[num]] - layout.data)
    for num, node in lin.items():
        key = lin_assign[num]
        if key is None:
            continue                          # keeps its own original data
        if key == MIRROR:                     # Windows: never shown there, share the twin's data
            point(node, win[num].flags, win[num].data_off)
        else:
            point(node, 0, placed[key] - layout.data)

    rec = MARKER + struct.pack(">I", len(meta)) + meta
    used_end = {pos: pos + 4 + len(blobs[i]) for i, pos in placed.items()}
    for s, e in regions:
        ends = [x for p, x in used_end.items() if s <= p < e]
        free_from = max(ends) if ends else s
        if e - free_from >= len(rec):
            fh.seek(free_from)
            fh.write(rec)
            return
    raise PatchError(tr("no_marker_space"))


def apply_patch(cfg, progress=lambda msg, frac=None: None):
    exe = cfg["exe"]
    if not os.path.isfile(exe):
        raise PatchError(tr("not_found", path=exe))
    if resolve_running():
        raise PatchError(tr("resolve_running"))
    if not cfg["images"]:
        raise PatchError(tr("no_images"))
    progress(tr("analyzing"), 0.02)
    version = file_version(exe)        # must be read before the file is opened for writing
    size = os.path.getsize(exe)

    # 1. back up pristine sets, roll previously patched sets back to the original layout
    with open(exe, "r+b") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            layouts = locate_all(mm)
            markers = {g: find_marker(mm, lay) for g, lay in layouts.items()}
            version, key = exe_identity(exe, mm, layouts, version)
            for g, lay in layouts.items():
                if markers[g] is None:
                    create_backup(mm, lay, key, version, size)
        for g, m in markers.items():
            if m is None:
                continue
            backup = load_backup(key, g, size)
            if not backup:
                raise PatchError(tr("patched_no_backup"))
            progress(tr("rollback"), 0.05)
            write_backup_into(fh, backup)
        fh.flush()

    # 2. read templates and free space of every set
    plan = {}
    with open(exe, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        for g, lay in layouts.items():
            if find_marker(mm, lay) is not None:
                raise PatchError(tr("rollback_failed"))
            win = lay.win_slots()
            template = Image.open(io.BytesIO(lay.read(mm, next(iter(win.values()))))).convert("RGBA")
            assignment = effective_slots(cfg, list(win))
            keep = [win[n] for n, v in assignment.items() if v is None]
            lin_template, lin_assign = _plan_linux(mm, lay, template, assignment, keep)
            plan[g] = {"layout": lay, "template": template, "assignment": assignment,
                       "lin_template": lin_template, "lin_assign": lin_assign,
                       "regions": _free_regions(mm, lay, keep)}

    images = {im["id"]: im for im in cfg["images"]}
    used = sorted({i for p in plan.values() for i in p["assignment"].values() if i is not None},
                  key=lambda i: list(images).index(i))
    if not used:
        raise PatchError(tr("all_original"))

    # 3. render every used image for every set
    total_steps = len(used) * len(plan)
    step = 0
    for k, img_id in enumerate(used):
        im = images[img_id]
        src = load_source(im["file"])
        for g, p in plan.items():
            step += 1
            progress(tr("preparing", name=im["name"], group=g), 0.08 + 0.72 * step / total_steps)
            for key, tpl in _blob_templates(p).items():
                if key.split(":")[0] != img_id:
                    continue
                out = render_splash(src, tpl, im["fx"], im["fy"], im["zoom"], cfg.get("darken", True))
                p.setdefault("rendered", {})[key] = out
                p.setdefault("blobs", {})[key] = encode_png(out)

    # 4. pack (quantise the heaviest images only if they don't fit)
    quantized = set()
    for g, p in plan.items():
        p["placed"] = _pack(p["blobs"], p["regions"])
        while p["placed"] is None:
            cand = [i for i in sorted(p["blobs"], key=lambda i: -len(p["blobs"][i]))
                    if (g, i) not in quantized]
            if not cand:
                raise PatchError(tr("no_space"))
            progress(tr("quantizing", name=images[cand[0].split(":")[0]]["name"]), 0.82)
            p["blobs"][cand[0]] = encode_png(p["rendered"][cand[0]], quantize=True)
            quantized.add((g, cand[0]))
            p["placed"] = _pack(p["blobs"], p["regions"])

    # 5. write
    progress(tr("writing"), 0.88)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(exe, "r+b") as fh:
        for g, p in plan.items():
            meta = json.dumps({"tool": APP_NAME, "time": stamp, "version": version, "group": g,
                               "slots": {str(k): v for k, v in p["assignment"].items()}}).encode()
            _write_group(fh, p["layout"], p["assignment"], p["lin_assign"], p["regions"], p["blobs"],
                         p["placed"], meta)

    # 6. verify: every slot must decode to an image of the original size
    progress(tr("verifying"), 0.96)
    with open(exe, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        for g, lay in layouts.items():
            if find_marker(mm, lay) is None:
                raise PatchError(tr("verify_marker", group=g))
            for num, node in lay.win_slots().items():
                img = Image.open(io.BytesIO(lay.read(mm, node)))
                img.load()
                if img.size != plan[g]["template"].size:
                    raise PatchError(tr("verify_slot", num=num, group=g))
            if not IS_WIN:      # on Linux the *_Linux variants are the ones that get shown
                for num, node in lay.linux_slots().items():
                    try:
                        img = Image.open(io.BytesIO(lay.read(mm, node)))
                        img.load()
                    except Exception:      # noqa: BLE001
                        raise PatchError(tr("verify_slot", num=f"Linux {num}", group=g))
                    if img.size != plan[g]["lin_template"].size:
                        log(f"warning: Linux slot {num} ({g}) is {img.size}, expected "
                            f"{plan[g]['lin_template'].size}")

    stats = {g: {"bytes": sum(len(b) for b in p["blobs"].values()),
                 "free": sum(e - s for s, e in p["regions"]), "size": list(p["template"].size)}
             for g, p in plan.items()}
    log(f"Patched {exe} ({version}): {len(used)} image(s), " +
        ", ".join(f"{g}: {s['bytes'] / 1e6:.1f}/{s['free'] / 1e6:.1f} MB" for g, s in stats.items()) +
        f", quantized={len(quantized)}")
    progress(tr("done"), 1.0)
    return {"images": len(used), "sets": stats,
            "quantized": sorted({images[i.split(":")[0]]["name"] for _, i in quantized})}


# --------------------------------------------------------------------------------------
# Scheduled task (auto re-patch after Resolve updates)
# --------------------------------------------------------------------------------------

def _python(windowless=True):
    exe = sys.executable
    if not IS_WIN:
        return exe
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe" if windowless else "python.exe")
    return cand if os.path.isfile(cand) else exe


_task_cache = (0.0, False)


def _systemctl(*args, user):
    cmd = ["systemctl", *(["--user"] if user else []), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _unit_dir(user):
    return os.path.expanduser("~/.config/systemd/user") if user else "/etc/systemd/system"


def task_installed():
    if not IS_WIN:
        global _task_cache
        if time.time() - _task_cache[0] < 5:
            return _task_cache[1]
        found = False
        if shutil.which("systemctl"):
            for user in (False, True):
                try:
                    found = found or _systemctl("is-enabled", f"{SERVICE_NAME}.path", user=user).returncode == 0
                except OSError:
                    pass
        _task_cache = (time.time(), found)
        return found
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0


def _install_task_linux():
    """A systemd .path unit watches the Resolve binary and starts `--auto` whenever it changes (an
    update replaces it); the service is also enabled at boot. System units when the binary needs
    root (this process then runs as root), user units otherwise."""
    global _task_cache
    if not shutil.which("systemctl"):
        raise PatchError(tr("task_failed", err="systemctl not found (systemd required)"))
    exe = load_config()["exe"]
    user = os.geteuid() != 0
    d = _unit_dir(user)
    os.makedirs(d, exist_ok=True)
    wanted = "default.target" if user else "multi-user.target"
    service = f"""[Unit]
Description=Re-apply custom DaVinci Resolve splash screens

[Service]
Type=oneshot
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={shlex.quote(_python())} {shlex.quote(os.path.abspath(__file__))} --auto --data-dir {shlex.quote(APP_DIR)}
TimeoutStartSec=15min

[Install]
WantedBy={wanted}
"""
    path_unit = f"""[Unit]
Description=Watch DaVinci Resolve for updates (Resolve Splash Patcher)

[Path]
PathChanged={exe}
Unit={SERVICE_NAME}.service

[Install]
WantedBy={wanted}
"""
    with open(os.path.join(d, f"{SERVICE_NAME}.service"), "w", encoding="utf-8") as f:
        f.write(service)
    with open(os.path.join(d, f"{SERVICE_NAME}.path"), "w", encoding="utf-8") as f:
        f.write(path_unit)
    for args in (("daemon-reload",), ("enable", f"{SERVICE_NAME}.service"),
                 ("enable", "--now", f"{SERVICE_NAME}.path")):
        r = _systemctl(*args, user=user)
        if r.returncode != 0:
            raise PatchError(tr("task_failed", err=(r.stderr or r.stdout).strip()))
    _task_cache = (0.0, False)
    return {"task": True}


def install_task():
    if not IS_WIN:
        return _install_task_linux()
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    script = os.path.abspath(__file__)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Re-applies custom DaVinci Resolve splash screens after updates</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT30S</Delay></LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Application"&gt;&lt;Select Path="Application"&gt;*[System[Provider[@Name='MsiInstaller'] and (EventID=1033 or EventID=11707 or EventID=1035 or EventID=11728)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_python()}</Command><Arguments>"{script}" --auto</Arguments></Exec></Actions>
</Task>"""
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-16") as f:
        f.write(xml)
    try:
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    finally:
        os.remove(path)
    if r.returncode != 0:
        raise PatchError(tr("task_failed", err=(r.stderr or r.stdout).strip()))
    return {"task": True}


def remove_task():
    global _task_cache
    if not IS_WIN:
        user = os.geteuid() != 0
        for suffix in ("path", "service"):
            _systemctl("disable", "--now", f"{SERVICE_NAME}.{suffix}", user=user)
            try:
                os.remove(os.path.join(_unit_dir(user), f"{SERVICE_NAME}.{suffix}"))
            except OSError:
                pass
        _systemctl("daemon-reload", user=user)
        _task_cache = (0.0, False)
        return {"task": False}
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    return {"task": False}


# --------------------------------------------------------------------------------------
# CLI (also used as the elevated worker of the interface)
# --------------------------------------------------------------------------------------

def _progress_writer(path):
    def write(msg, frac=None, **extra):
        if not path:
            log(msg)
            return
        data = {"msg": msg, "frac": frac, **extra}
        tmp = path + ".tmp"
        # the interface may be reading the file at this very moment (Windows sharing
        # violation), so retry; progress reporting must never break the operation itself
        for _ in range(40):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                return
            except OSError:
                time.sleep(0.05)
        if extra.get("done"):
            log("could not write final progress file")
    return write


def _apply_data_dir(argv):
    if "--data-dir" in argv:
        _set_app_dir(argv[argv.index("--data-dir") + 1])


def _dir_owner():
    """Linux, running as root on behalf of a user: remember who owns the data folder."""
    if IS_WIN or os.geteuid() != 0:
        return None
    try:
        st = os.stat(APP_DIR)
    except OSError:
        return None
    return (st.st_uid, st.st_gid) if st.st_uid != 0 else None


def _restore_ownership(owner):
    """Files the root worker created in the user's data folder go back to the user."""
    if not owner:
        return
    for dp, dns, fns in os.walk(APP_DIR):
        for name in dns + fns:
            try:
                os.lchown(os.path.join(dp, name), *owner)
            except OSError:
                pass


def cli(argv):
    _apply_data_dir(argv)
    owner = _dir_owner()
    try:
        return _cli(argv)
    finally:
        _restore_ownership(owner)


def _cli(argv):
    pfile = argv[argv.index("--progress") + 1] if "--progress" in argv else None
    progress = _progress_writer(pfile)
    cfg = load_config()
    try:
        if "--find" in argv:
            found = find_resolve_binary()
            print(found or "No Resolve binary with splash screens found")
            return 0 if found else 1
        if "--check" in argv:
            print_check(cfg["exe"])
            return 0
        if "--diagnose" in argv:
            diagnose(cfg["exe"])
            return 0
        if "--restore" in argv:
            res = restore_original(cfg["exe"], progress)
        elif "--task-on" in argv:
            res = install_task()
        elif "--task-off" in argv:
            res = remove_task()
        elif "--auto" in argv:
            if not cfg["images"]:
                return 0
            for _ in range(20):          # the installer may still hold the file
                if os.path.isfile(cfg["exe"]) and not resolve_running():
                    try:
                        info = inspect_exe(cfg["exe"])
                        break
                    except (OSError, PatchError):
                        pass
                time.sleep(15)
            else:
                log("auto: Resolve.exe not accessible, giving up")
                return 1
            if info.state == "patched":
                return 0
            log(f"auto: Resolve {info.version} is not fully patched – patching")
            res = apply_patch(cfg, progress)
        else:
            res = apply_patch(cfg, progress)
        progress(tr("done"), 1.0, done=True, result=res)
        return 0
    except Exception as e:
        log("ERROR: " + "".join(traceback.format_exception(e)))
        progress(str(e), None, done=True, error=str(e))
        return 1


# --------------------------------------------------------------------------------------
# Interface: local HTTP server + Edge app window
# --------------------------------------------------------------------------------------

if IS_WIN:
    class _SEI(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND),
                    ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                    ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                    ("dwHotKey", wt.DWORD), ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE)]


def _run_worker_posix(script, args, progress_file):
    cmd = [sys.executable, script, *args, "--progress", progress_file, "--data-dir", APP_DIR]
    if not needs_elevation(load_config()["exe"]):
        return subprocess.run(cmd).returncode
    # no bytecode: root must not leave root-owned __pycache__ folders in the user's venv
    cmd = ["env", "PYTHONDONTWRITEBYTECODE=1", *cmd]
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("pkexec"):
        code = subprocess.run(["pkexec", *cmd]).returncode
        if code in (126, 127) and not os.path.exists(progress_file):
            raise PatchError(tr("uac_denied"))      # dialog dismissed / not authorised
        return code
    if shutil.which("sudo") and sys.stdin and sys.stdin.isatty():
        return subprocess.run(["sudo", *cmd]).returncode
    raise PatchError(tr("no_elevation", cmd="sudo " + shlex.join(cmd)))


def run_worker(args, progress_file):
    """Runs this script with `args` (elevated via UAC if needed) and waits. Returns exit code."""
    script = os.path.abspath(__file__)
    if not IS_WIN:
        return _run_worker_posix(script, args, progress_file)
    full = [script, *args, "--progress", progress_file]
    if is_admin():
        return subprocess.run([_python(False), *full], creationflags=CREATE_NO_WINDOW).returncode
    sei = _SEI()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = _python()
    sei.lpParameters = subprocess.list2cmdline(full)
    sei.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        if ctypes.GetLastError() == 1223:
            raise PatchError(tr("uac_denied"))
        raise PatchError(tr("uac_failed"))
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = wt.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return code.value


def _is_snap(path):
    real = os.path.realpath(path)
    return real.startswith("/snap/") or os.path.basename(real) == "snap"


def find_browser():
    """Chromium-family browser for the frameless --app window (Edge on Windows)."""
    if not IS_WIN:
        for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
                     "microsoft-edge-stable", "microsoft-edge", "brave-browser", "vivaldi"):
            path = shutil.which(name)
            if path:
                return path
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(p):
                return p
    return None


class App:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.info = None
        self.info_error = None
        self.orig = {}
        self.template_png = None
        self.box = None
        self.src_cache = {}
        self.job = {"running": False}
        self.last_ping = time.time()
        self.bye_at = None
        self.refresh()

    # ---- exe state
    def refresh(self, auto_search=False):
        with self.lock:
            try:
                self.info = inspect_exe(self.cfg["exe"])
                self.info_error = None
                self.orig = original_slot_pngs(self.info)
                first = self.orig[min(self.orig)] if self.orig else None
                if first:
                    tpl = Image.open(io.BytesIO(first)).convert("RGBA")
                    self.box = list(template_geometry(tpl))
                    bio = io.BytesIO()
                    tpl.save(bio, "PNG")
                    self.template_png = bio.getvalue()
            except Exception as e:
                self.info, self.orig = None, {}
                self.info_error = str(e) if os.path.isfile(self.cfg["exe"]) else \
                    tr("exe_missing")
                if not IS_WIN and not auto_search:
                    # wrong file or install in a non-default place: look for the binary ourselves
                    found = find_resolve_binary()
                    if found and found != self.cfg["exe"]:
                        self.cfg["exe"] = found
                        save_config(self.cfg)
                        return self.refresh(auto_search=True)

    def state(self):
        info = self.info
        slots = list(info.layouts[next(iter(info.layouts))].win_slots()) if info else []
        sets = []
        if info:
            for g, lay in info.layouts.items():
                first = next(iter(lay.win_slots().values()))
                sets.append({"id": g, "slots": len(lay.win_slots()), "patched": info.markers[g] is not None})
        return {
            "exe": self.cfg["exe"],
            "error": self.info_error,
            "version": info.version if info else None,
            "state": info.state if info else None,
            "patchedTime": info.patched_time if info else None,
            "backupOk": bool(info) and all(info.backup_ok(g) for g, m in info.markers.items() if m),
            "sets": sets,
            "slots": slots,
            "box": self.box,
            "images": [{k: im[k] for k in ("id", "name", "fx", "fy", "zoom")} for im in self.cfg["images"]],
            "darken": self.cfg.get("darken", True),
            "lang": self.cfg.get("lang", "en"),
            "slotCfg": self.cfg["slots"],
            "task": task_installed(),
            "running": resolve_running(),
            "admin": is_admin(),
            "platform": "windows" if IS_WIN else "linux",
        }

    def source_jpeg(self, img_id):
        with self.lock:
            if img_id not in self.src_cache:
                im = next(i for i in self.cfg["images"] if i["id"] == img_id)
                src = load_source(im["file"], max_side=3000)
                bio = io.BytesIO()
                src.save(bio, "JPEG", quality=92)
                self.src_cache[img_id] = bio.getvalue()
            return self.src_cache[img_id]

    # ---- jobs
    def start_job(self, kind, args):
        if self.job.get("running"):
            raise PatchError(tr("busy"))
        save_config(self.cfg)
        # Linux: inside the data folder, so the root worker can write it and we can still delete it
        pdir = tempfile.gettempdir() if IS_WIN else APP_DIR
        os.makedirs(pdir, exist_ok=True)
        pfile = os.path.join(pdir, f"rsp_{uuid.uuid4().hex[:8]}.json")
        self.job = {"running": True, "kind": kind, "msg": tr("waiting_admin"), "frac": 0}

        def worker():
            try:
                code = run_worker(args, pfile)
                data = {}
                try:
                    with open(pfile, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    pass
                if code != 0 and not data.get("error"):
                    data["error"] = tr("op_failed")
                self.job = {"running": False, "kind": kind, "msg": data.get("msg", ""), "frac": 1,
                            "error": data.get("error"), "result": data.get("result")}
            except Exception as e:
                self.job = {"running": False, "kind": kind, "msg": str(e), "error": str(e)}
            finally:
                try:
                    os.remove(pfile)
                except OSError:
                    pass
                self.refresh()

        def poll():
            while self.job.get("running"):
                try:
                    with open(pfile, encoding="utf-8") as f:
                        data = json.load(f)
                    if self.job.get("running"):
                        self.job.update(msg=data.get("msg", ""), frac=data.get("frac"))
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)

        threading.Thread(target=worker, daemon=True).start()
        threading.Thread(target=poll, daemon=True).start()


def pick_exe_dialog(initial):
    """Native file dialog (runs in its own thread with its own Tk root)."""
    if not IS_WIN:
        start = initial if initial and os.path.exists(initial) else "/opt/resolve/bin/"
        for cmd in (["zenity", "--file-selection", "--title", tr("pick_exe"), "--filename", start],
                    ["kdialog", "--title", tr("pick_exe"), "--getopenfilename", start]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True)
                out = r.stdout.strip()
                return out if r.returncode == 0 and out else None
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise PatchError(tr("no_dialog"))
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    types = [(TARGET_NAME, TARGET_NAME)] if IS_WIN else [("All files", "*")]
    p = filedialog.askopenfilename(title=tr("pick_exe"), filetypes=types,
                                   initialdir=os.path.dirname(initial) if initial else None)
    root.destroy()
    return os.path.normpath(p) if p else None


def run_server(open_window=True):
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from urllib.parse import unquote

    app = App()
    token = secrets.token_urlsafe(12)
    dialog_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body=b"", ctype="application/json", cache=False):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if not parts or parts[0] != token:
                return None
            return [unquote(p) for p in parts[1:]]

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            r = self.route()
            if r is None:
                return self.send(404, {"error": "not found"})
            try:
                if r == [] or r == [""]:
                    with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as f:
                        return self.send(200, f.read(), "text/html; charset=utf-8")
                if r == ["api", "state"]:
                    return self.send(200, app.state())
                if r == ["api", "job"]:
                    return self.send(200, app.job)
                if r == ["api", "template.png"]:
                    return self.send(200, app.template_png or b"", "image/png")
                if len(r) == 3 and r[:2] == ["api", "orig"]:
                    data = app.orig.get(int(r[2]))
                    return self.send(200, data, "image/png", cache=True) if data else self.send(404)
                if len(r) == 3 and r[:2] == ["api", "src"]:
                    return self.send(200, app.source_jpeg(r[2]), "image/jpeg", cache=True)
                return self.send(404, {"error": "not found"})
            except Exception as e:
                return self.send(500, {"error": str(e)})

        def do_POST(self):
            r = self.route()
            if r is None:
                return self.send(404, {"error": "not found"})
            app.last_ping = time.time()
            try:
                if r == ["api", "ping"]:
                    app.bye_at = None
                    return self.send(200, {"ok": True})
                if r == ["api", "bye"]:
                    app.bye_at = time.time()
                    return self.send(200, {"ok": True})
                if r == ["api", "upload"]:
                    name = unquote(self.headers.get("X-Filename", "image.png"))
                    im = import_image_bytes(self.body(), os.path.basename(name))
                    with app.lock:
                        app.cfg["images"].append(im)
                        save_config(app.cfg)
                    return self.send(200, {"id": im["id"]})
                if r == ["api", "config"]:
                    data = json.loads(self.body() or b"{}")
                    with app.lock:
                        by_id = {i["id"]: i for i in app.cfg["images"]}
                        if "images" in data:   # order + per-image settings
                            new = []
                            for d in data["images"]:
                                im = by_id.get(d["id"])
                                if im:
                                    for k in ("fx", "fy", "zoom"):
                                        im[k] = float(d[k])
                                    new.append(im)
                            app.cfg["images"] = new
                        if "slots" in data:
                            app.cfg["slots"] = data["slots"]
                        if "darken" in data:
                            app.cfg["darken"] = bool(data["darken"])
                        if data.get("lang") in ("en", "ru"):
                            app.cfg["lang"] = data["lang"]
                            set_lang(data["lang"])
                        save_config(app.cfg)
                    return self.send(200, {"ok": True})
                if len(r) == 3 and r[:2] == ["api", "delete"]:
                    with app.lock:
                        im = next((i for i in app.cfg["images"] if i["id"] == r[2]), None)
                        if im:
                            app.cfg["images"].remove(im)
                            app.cfg["slots"] = {k: ("auto" if v == im["id"] else v)
                                                for k, v in app.cfg["slots"].items()}
                            save_config(app.cfg)
                            app.src_cache.pop(im["id"], None)
                            try:
                                os.remove(im["file"])
                            except OSError:
                                pass
                    return self.send(200, {"ok": True})
                if r == ["api", "exe"]:
                    data = json.loads(self.body() or b"{}")
                    path = data.get("path")
                    if data.get("browse"):
                        with dialog_lock:
                            path = pick_exe_dialog(app.cfg["exe"])
                    if path:
                        with app.lock:
                            app.cfg["exe"] = path
                            save_config(app.cfg)
                        app.refresh()
                    return self.send(200, app.state())
                if r == ["api", "refresh"]:
                    app.refresh()
                    return self.send(200, app.state())
                if r == ["api", "apply"]:
                    app.start_job("apply", ["--apply"])
                    return self.send(200, app.job)
                if r == ["api", "restore"]:
                    app.start_job("restore", ["--restore"])
                    return self.send(200, app.job)
                if r == ["api", "task"]:
                    on = json.loads(self.body() or b"{}").get("on")
                    app.start_job("task", ["--task-on" if on else "--task-off"])
                    return self.send(200, app.job)
                return self.send(404, {"error": "not found"})
            except PatchError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:
                log("ERROR: " + "".join(traceback.format_exception(e)))
                return self.send(500, {"error": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/{token}/"
    log(f"Interface at {url}")

    def watchdog():
        while True:
            time.sleep(2)
            if app.job.get("running"):
                continue
            idle = time.time() - app.last_ping
            if (app.bye_at and time.time() - app.bye_at > 6) or idle > 180:
                srv.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if open_window:
        edge = find_browser()
        if edge:
            flags = [f"--app={url}", "--window-size=1500,980",
                     "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]
            if not (not IS_WIN and _is_snap(edge)):      # snap confinement can't use a dot-folder profile
                flags.append(f"--user-data-dir={os.path.join(APP_DIR, 'window')}")
            subprocess.Popen([edge, *flags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
    srv.serve_forever()


def main():
    argv = sys.argv[1:]
    _apply_data_dir(argv)
    if any(a in argv for a in ("--apply", "--auto", "--restore", "--task-on", "--task-off",
                               "--check", "--find", "--diagnose")):
        sys.exit(cli(argv))
    run_server(open_window="--no-window" not in argv)


if __name__ == "__main__":
    main()
