"""Bose Soundbar remote: customtkinter UI on top of pybose (https://github.com/cavefire/pybose)."""
import asyncio
import ctypes
import gc
import json
import logging
import logging.handlers
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter as ctk

from pybose.BoseAuth import BoseAuth
from pybose.BoseDiscovery import BoseDiscovery
from pybose.BoseSpeaker import BoseSpeaker

try:
    from pybose.BoseAuth import BoseAuthRejectedError, BoseAuthUnavailableError
except ImportError:  # older pybose: these failures were plain ValueErrors
    class BoseAuthRejectedError(ValueError):
        pass

    class BoseAuthUnavailableError(ValueError):
        pass

import icons
import updater
from build_info import REPO
from build_info import VERSION as BUILD_VERSION
from PIL import Image as _PILImage

# Pillow imports its PNG/JPEG/... plugins lazily on first use. Covers are decoded on worker threads while the UI
# thread also uses Pillow, and two threads doing that first import at once can deadlock on Python's import lock
# (seen in CI as a hang). Loading them here, once, on the main thread, removes the race.
_PILImage.init()

APP_NAME = "Bose Soundbar"
APP_VERSION = "1.4.0"  # the version series; the release workflow stamps the full version (1.4.<build>) via build_info
if BUILD_VERSION:
    APP_VERSION = BUILD_VERSION
PROJECT_URL = f"https://github.com/{REPO or 'amidstdebug/bose-device-control'}"  # this project
PYBOSE_URL = "https://github.com/cavefire/pybose"  # the library that talks to the speaker
AUMID = "BoseSoundbarRemote.App"  # gives the taskbar its own identity/icon instead of Python's
SINGLE_INSTANCE_PORT = int(os.environ.get("BOSE_REMOTE_PORT") or 47653)
POLL_SECONDS = 5  # safety-net refresh interval (tests shorten it)
HIDDEN_POLL_FACTOR = 6     # minimized / in the tray: poll this many times less often (pushes still arrive live)
MAX_RETRY_SECONDS = 60     # ceiling for the back-off while the speaker can't be reached
FIRST_RETRY_SECONDS = 10   # first background retry after a failed connection attempt; doubles from there
LOGIN_RETRY_SECONDS = 5    # first retry when the login can't be renewed because there is no internet yet
CONNECT_TIMEOUT = 25       # seconds before a connection attempt is abandoned
SUBNET_SCAN_EVERY = 600    # while retrying in the background, port-scan the subnet at most this often (seconds)
MAX_ERROR_LINES = 300      # the error panel keeps only this many lines

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
CAN_SELF_UPDATE = IS_WIN   # one-click updates run the Windows installer; elsewhere the banner links to the release


def resource(rel):
    """Path to a bundled file, both from source and from the PyInstaller exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / rel


ICON = resource("assets/icon.ico")

def default_data_dir():
    """Per-user data dir, the same from source and from the packaged app: %APPDATA% on Windows, XDG config on Linux."""
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home()) / "BoseSoundbarRemote"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "BoseSoundbarRemote"


DATA_DIR = Path(os.environ.get("BOSE_REMOTE_DATA_DIR") or default_data_dir())
DATA_DIR.mkdir(parents=True, exist_ok=True)
if not IS_WIN:
    try:
        DATA_DIR.chmod(0o700)  # it holds login tokens
    except OSError:
        pass
CONFIG = DATA_DIR / "bose_config.json"  # tokens, speaker address, settings (never the password)
LOGFILE = DATA_DIR / "bose_app.log"

_OLD_CONFIG = Path(__file__).parent / "bose_config.json"  # from earlier dev runs
if _OLD_CONFIG.exists() and not CONFIG.exists():
    shutil.move(str(_OLD_CONFIG), str(CONFIG))



class _DedupFilter(logging.Filter):
    """Drops a message that repeats the previous one, then says how often it repeated.

    A speaker that is switched off produces the same failure on every retry, for days if the app sits in the tray;
    without this the log is the same line a hundred thousand times.
    """

    def __init__(self, window=600):
        super().__init__()
        self.window, self._last, self._count, self._since = window, None, 0, 0.0

    def filter(self, record):
        try:
            key = (record.levelno, record.getMessage())
        except Exception:
            return True
        now = time.monotonic()
        if key == self._last and now - self._since < self.window:
            self._count += 1
            return False
        if self._count:
            record.msg = f"(previous message repeated {self._count} more times)\n{record.getMessage()}"
            record.args = None
        self._last, self._count, self._since = key, 0, now
        return True


def setup_logging():
    """Log to a file that can never grow without bound: 1 MB, two older files kept."""
    handler = logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.addFilter(_DedupFilter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")
    # these libraries narrate every connection at INFO; only their problems are interesting
    for noisy in ("websockets", "zeroconf", "pybose", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()

# (light, dark) tuples: customtkinter switches between them live when the appearance mode changes
BG = ("#f1f3f6", "#0f1115")
CARD = ("#ffffff", "#181b22")
BTN = ("#e6e9ef", "#0f1115")
HOVER = ("#d5d9e2", "#232834")
MUTED = ("#6b7280", "#8b93a1")
TEXT = ("#111827", "#e8eaed")
RED = ("#dc2626", "#f87171")
GREEN = ("#16a34a", "#4ade80")

# glyph in the Windows icon fonts -> the drawn icon (icons.py) used where those fonts don't exist
ICON_NAMES = {"\ue713": "settings", "\ue7e8": "power", "\ue892": "prev", "\ue893": "next", "\ue768": "play",
              "\ue769": "pause", "\ue767": "volume", "\ue74f": "muted", "\ue72b": "back"}

ACCENTS = {
    "Blue": "#3b82f6", "Teal": "#14b8a6", "Green": "#22c55e", "Purple": "#8b5cf6",
    "Pink": "#ec4899", "Orange": "#f97316", "Red": "#ef4444",
}

DEFAULTS = {
    "device_name": "",          # empty = use the name stored on the speaker
    "appearance": "Dark",       # Dark / Light / System
    "accent": "Blue",
    "close_action": "Minimize to taskbar",  # or "Minimize to tray" / "Exit"
    "start_minimized": True,    # when launched by the Windows startup shortcut
    "always_on_top": False,
    "remember_position": True,
    "show_log": False,          # error-message panel is opt-in
    "keyboard_shortcuts": True,
    "max_volume": 100,
    "volume_step": 5,
    "check_updates": True,     # look for a newer release on GitHub at startup
    "hidden_sources": [],      # "SOURCE|account" keys the user hid from the main screen
    "ui_scale": 100,           # interface size in percent (Linux; Windows follows the display scaling by itself)
    "window_pos": "",
}
CLOSE_ACTIONS = ["Minimize to taskbar", "Minimize to tray", "Exit"]


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_config_lock = threading.Lock()


def save_config(cfg):
    """Write the config atomically (temp file + rename), so a crash or power cut mid-write can't leave a half-written
    file that logs the user out and resets every setting. Safe to call from any thread. Returns True on success."""
    with _config_lock:
        for _attempt in range(3):
            try:
                text = json.dumps(cfg, indent=2)
                break
            except RuntimeError:  # another thread changed the dict while it was being serialised
                time.sleep(0.01)
        else:
            return False
        tmp = CONFIG.with_name(CONFIG.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            if not IS_WIN:
                tmp.chmod(0o600)  # tokens: readable by this user only
            for attempt in range(4):
                try:
                    os.replace(tmp, CONFIG)
                    break
                except PermissionError:  # Windows: a virus scanner or backup tool has the file open for a moment
                    if attempt == 3:
                        raise
                    time.sleep(0.05)
            return True
        except OSError as e:
            logging.warning("could not save settings: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False


def darken(hex_color, f=0.82):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


# ---------------------------------------------------------------- shortcuts / autostart
DESKTOP_FILE = "bose-soundbar-remote.desktop"  # Linux launcher file name


def _known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def _xdg_desktop_dir():
    """The user's Desktop folder on Linux (it is translated on non-English systems, e.g. ~/Bureau)."""
    try:
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Desktop"


def shortcut_path(kind):
    if kind not in ("desktop", "start", "startup"):
        raise ValueError(kind)
    if not IS_WIN:
        if kind == "desktop":
            return _xdg_desktop_dir() / DESKTOP_FILE
        if kind == "start":
            return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications" / DESKTOP_FILE
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / DESKTOP_FILE
    name = f"{APP_NAME}.lnk"
    if kind == "desktop":
        return _known_folder(0x10) / name                       # CSIDL_DESKTOPDIRECTORY
    if kind == "start":
        return _known_folder(0x02) / name                       # CSIDL_PROGRAMS (user Start menu > Programs)
    return _known_folder(0x07) / name                           # CSIDL_STARTUP


def shortcut_exists(kind):
    try:
        return shortcut_path(kind).exists()
    except Exception:  # no such folder on this system
        return False


def launch_spec():
    """(target, arguments, working dir, icon) that launches this app."""
    if getattr(sys, "frozen", False):
        icon = f"{sys.executable},0" if IS_WIN else str(linux_icon_file())
        return sys.executable, "", str(Path(sys.executable).parent), icon
    script = Path(__file__).resolve()
    if IS_WIN:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        return str(pythonw), f'"{script}"', str(script.parent), str(ICON)
    return sys.executable, f'"{script}"', str(script.parent), str(linux_icon_file())


def linux_icon_file():
    """A PNG of the app icon for .desktop files (they can't use .ico); written once into the data folder."""
    png = DATA_DIR / "icon.png"
    if not png.exists():
        try:
            img = _PILImage.open(ICON)
            img.size = max(img.info.get("sizes", [img.size]))  # the largest picture stored in the .ico
            img.convert("RGBA").save(png, "PNG")
        except Exception as e:
            logging.info("could not write the launcher icon: %s", e)
    return png


def _desktop_quote(arg):
    """Quote one argument for the Exec= key of a .desktop file."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'


def desktop_entry(target, args, workdir, icon, minimized=False):
    """Contents of a freedesktop.org launcher (.desktop file) for this app."""
    exec_line = _desktop_quote(target) + (f" {args}" if args else "") + (" --minimized" if minimized else "")
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Version=1.0", f"Name={APP_NAME}",
        "GenericName=Speaker remote", "Comment=Remote control for Bose speakers and soundbars",
        f"Exec={exec_line}", f"Path={workdir}", f"Icon={icon}", "Terminal=false",
        "Categories=AudioVideo;Audio;", "StartupWMClass=BoseSoundbarRemote", "StartupNotify=false",
        *(["X-GNOME-Autostart-enabled=true"] if minimized else []), "",
    ])


def create_shortcut(kind):
    target, args, workdir, icon = launch_spec()
    lnk = shortcut_path(kind)
    lnk.parent.mkdir(parents=True, exist_ok=True)
    if not IS_WIN:
        lnk.write_text(desktop_entry(target, args, workdir, icon, minimized=kind == "startup"), encoding="utf-8")
        lnk.chmod(0o755)  # desktops refuse to launch a .desktop file that isn't executable
        if kind == "desktop":  # GNOME additionally wants the file marked as trusted
            try:
                subprocess.run(["gio", "set", str(lnk), "metadata::trusted", "true"], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        return
    if kind == "startup":
        args = (args + " --minimized").strip()
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:B_LNK);"
          "$s.TargetPath=$env:B_TGT;$s.Arguments=$env:B_ARGS;$s.WorkingDirectory=$env:B_WD;"
          "$s.IconLocation=$env:B_ICON;$s.Description='Bose Soundbar Remote';$s.Save()")
    env = {**os.environ, "B_LNK": str(lnk), "B_TGT": target, "B_ARGS": args, "B_WD": workdir, "B_ICON": icon}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       capture_output=True, text=True, creationflags=0x08000000, timeout=60)
    if r.returncode != 0 or not lnk.exists():
        raise RuntimeError(r.stderr.strip() or "PowerShell failed to create the shortcut")


def remove_shortcut(kind):
    shortcut_path(kind).unlink(missing_ok=True)


def open_path(path):
    """Open a folder or file in the desktop's file manager / default app."""
    if IS_WIN:
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def tray_available():
    """Can a tray icon actually be shown? On Windows always. On Linux only if something owns the X11 system tray;
    plain GNOME has none, and hiding the window there would leave no way to get it back."""
    if IS_WIN:
        return True
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import X
        from Xlib import display as xdisplay
        d = xdisplay.Display()
        try:
            atom = d.intern_atom(f"_NET_SYSTEM_TRAY_S{d.get_default_screen()}")
            return d.get_selection_owner(atom) != X.NONE
        finally:
            d.close()
    except Exception as e:
        logging.info("no system tray: %s", e)
        return False


# ---------------------------------------------------------------- networking helper
def scan_subnet(port=8082, prefix=None):
    """Find hosts with the Bose websocket port open on this PC's /24 (fallback when mDNS is blocked).

    `prefix` is the first three octets ("192.168.1"); by default it comes from the active network interface.
    """
    if prefix is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # no packets sent; just picks the outgoing interface
            prefix = s.getsockname()[0].rsplit(".", 1)[0]
        finally:
            s.close()

    def check(i):
        ip = f"{prefix}.{i}"
        c = socket.socket()
        c.settimeout(0.8)
        try:
            c.connect((ip, port))
            return ip
        except OSError:
            return None
        finally:
            c.close()

    with ThreadPoolExecutor(64) as ex:
        return [ip for ip in ex.map(check, range(1, 255)) if ip]


def acquire_single_instance(port=None):
    """Listening socket if we're the first instance; otherwise ask the running one to show itself."""
    port = port or SINGLE_INSTANCE_PORT
    srv = socket.socket()
    try:
        if IS_WIN:  # without this, a second process could bind the same port on Windows and both would run
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(2)
        return srv
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None


# ---------------------------------------------------------------- app
class Btn(ctk.CTkButton):
    """CTkButton that doesn't reserve corner_radius of empty space on each side of its label,
    so big-radius round/pill buttons keep the width you ask for."""

    def __init__(self, *args, side_pad=6, **kwargs):
        self._side_pad = side_pad
        super().__init__(*args, **kwargs)

    def _create_grid(self):
        super()._create_grid()
        pad = self._apply_widget_scaling(self._side_pad)
        self.grid_columnconfigure(0, minsize=pad)
        self.grid_columnconfigure(4, minsize=pad)


def _round_square(img, size, k=3):
    """Round the corners of a square RGBA image (supersampled for smooth edges), return it at `size`."""
    from PIL import Image, ImageDraw
    corner = Image.new("L", (size * k, size * k), 0)
    ImageDraw.Draw(corner).rounded_rectangle((0, 0, size * k - 1, size * k - 1), radius=44 * k, fill=255)
    img = img.copy()
    img.putalpha(corner)
    return img.resize((size, size), Image.LANCZOS)


def to_ctk(img):
    return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)


def make_art_pil(accent, size=170):
    """Rounded gradient tile used as the 'album art' placeholder."""
    from PIL import Image
    k = 3
    top = Image.new("RGB", (size * k, size * k), accent)
    bottom = Image.new("RGB", (size * k, size * k), darken(accent, 0.38))
    mask = Image.linear_gradient("L").resize((size * k, size * k))
    return _round_square(Image.composite(bottom, top, mask).convert("RGBA"), size, k)


def art_pil_from_bytes(data, size=170):
    """Square-cropped, rounded album cover (PIL RGBA) from raw image bytes."""
    import io
    from PIL import Image
    k = 3
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    m = min(w, h)
    left, top = (w - m) // 2, (h - m) // 2
    img = img.crop((left, top, left + m, top + m)).resize((size * k, size * k), Image.LANCZOS).convert("RGBA")
    return _round_square(img, size, k)


def make_art(accent, size=170):
    return to_ctk(make_art_pil(accent, size))


def art_from_bytes(data, size=170):
    return to_ctk(art_pil_from_bytes(data, size))


def fetch_image(url, limit=6_000_000):
    import requests
    if not url.lower().startswith("https://"):
        raise ValueError("refusing non-https image url")
    r = requests.get(url, timeout=8, stream=True)
    try:
        r.raise_for_status()
        data = r.raw.read(limit + 1, decode_content=True)
    finally:
        r.close()  # a streamed response keeps its connection until closed
    if len(data) > limit:
        raise ValueError("image too large")
    return data


def system_theme():
    """"Dark" or "Light", whatever the desktop is set to."""
    try:
        import darkdetect
        return "Dark" if darkdetect.theme() == "Dark" else "Light"
    except Exception:
        return "Light"


def set_ui_scale(percent):
    """Interface size for Linux, where customtkinter doesn't follow the display scaling by itself."""
    try:
        scale = max(0.5, min(3.0, int(percent or 100) / 100))
    except (TypeError, ValueError):
        scale = 1.0
    tracker = getattr(ctk, "ScalingTracker", None)
    if tracker is not None and getattr(tracker, "widget_scaling", None) == scale \
            and getattr(tracker, "window_scaling", None) == scale:
        return  # nothing to change; rescaling redraws every widget
    ctk.set_widget_scaling(scale)
    ctk.set_window_scaling(scale)


def tune_toolkit():
    """customtkinter runs two housekeeping timers for as long as a window exists: one looks for a dark/light switch
    every 30 ms, one for a display-scaling change every 100 ms. Nobody needs either that often, and they keep the
    process waking up dozens of times a second even while it sits in the tray."""
    for name, interval in (("AppearanceModeTracker", 1000), ("ScalingTracker", 500)):
        tracker = getattr(ctk, name, None)
        if tracker is not None and hasattr(tracker, "update_loop_interval"):
            tracker.update_loop_interval = interval


class App(ctk.CTk):
    def __init__(self, instance_sock, start_hidden):
        self.cfg = load_config()
        self.s = {**DEFAULTS, **self.cfg.get("settings", {})}
        tune_toolkit()
        # Outside Windows, "System" is resolved here and re-checked in the background (see _watch_system_theme):
        # customtkinter's own check starts a `gsettings` process from the UI thread on every tick.
        self._sys_theme = "Light" if IS_WIN or self.s["appearance"] != "System" else system_theme()
        ctk.set_appearance_mode(self._appearance())
        if IS_LINUX:
            set_ui_scale(self.s.get("ui_scale"))
        # className sets WM_CLASS, which is how Linux desktops match the window to its launcher/icon
        super().__init__(fg_color=BG, **({} if IS_WIN else {"className": "BoseSoundbarRemote"}))
        if os.environ.get("BOSE_REMOTE_TEST_HIDDEN"):  # automated tests: fully transparent, no taskbar button
            self.attributes("-alpha", 0.0)
            if IS_WIN:
                self.attributes("-toolwindow", True)
        self._visible = True       # False while minimized or hidden in the tray: timers and polling slow down
        self._closing = False
        self._timers = {}          # name -> id of each repeating timer, so quitting can cancel them
        self._icon_photo = None
        self._icon_cache = {}
        self._init_fonts()
        self.accent = ACCENTS.get(self.s["accent"], ACCENTS["Blue"])
        self.accent_hover = darken(self.accent)

        sh = self.winfo_screenheight()
        self.geometry(f"500x{min(880, sh - 90)}")
        self.minsize(440, min(740, sh - 90))
        self._restore_position()
        self.attributes("-topmost", bool(self.s["always_on_top"]))
        self._set_icon()

        self.auth = BoseAuth()
        self.speaker = None
        self.speaker_name = None
        self.muted = False
        self.playing = False
        self.power_on = True
        self.cur_vol = 0
        self.cur_source = None
        self.sources = []          # chips currently shown (after hiding)
        self.all_sources = []      # every selectable source the speaker reports
        self._art_url = None
        self._art_cache = {}
        self._art_pil = None      # what the cover label currently shows (for cross-fading)
        self._art_job = None
        self.update_info = None   # newer release found on GitHub, if any
        self._pos_base = 0.0      # seconds into the track at the last sync
        self._pos_t0 = 0.0
        self._dur = 0
        self._can_seek = False
        self._seek_job = None
        self._seeking = False
        self._playing_now = False
        self._last_src_seen = None
        self._picked = {}  # source -> account the user last chose, to pick the right chip
        self._last = None
        self._volume_job = None
        self._refresh_pending = False
        self._refresh_seq = 0      # numbers each refresh by when it started...
        self._applied_seq = 0      # ...so a slow, older one can never overwrite a newer result
        self._view = None
        self._main_view = "login"
        self._status = ("Starting...", MUTED, MUTED)
        self.tray = None
        self._prog_shown = False
        self._instance_sock = instance_sock
        self._conn_gen = 0         # bumped by every connect/disconnect, so stale retries know to stop
        self._retry_n = 0
        self._last_scan = 0.0
        self._conn_lock = asyncio.Lock()  # one connection attempt at a time (only ever used on the asyncio thread)
        self._gc_runs = 0

        self._ui_queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="bose-asyncio").start()
        self.report_callback_exception = self._tk_exception

        self._build_all()
        self._drain_ui()
        self._bind_keys()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Map>", self._on_map_change, add=True)
        self.bind("<Unmap>", self._on_map_change, add=True)

        # Tk objects (fonts, images, variables) talk to Tk when they are freed, and Tk may only be used from this
        # thread. Python's garbage collector runs on whichever thread happens to allocate, so left alone it would
        # free them on the network thread, which then stalls one second per object whenever this thread is busy.
        # So: automatic collection off, and this thread collects on a timer instead.
        gc.disable()
        self._every("gc", 5000, self._gc_tick)

        if instance_sock:
            threading.Thread(target=self._instance_listener, args=(instance_sock,), daemon=True,
                             name="bose-single-instance").start()
        if not IS_WIN:
            self.run(self._watch_system_theme(), "Theme watcher failed")
        if start_hidden:
            self.after(400, self._minimize_on_start)
        self.run(self._startup())

    # ------------------------------------------------------------ fonts / icons
    def _appearance(self):
        """The mode to hand to customtkinter: the setting itself, except "System" outside Windows (resolved here)."""
        mode = self.s["appearance"]
        return self._sys_theme if (mode == "System" and not IS_WIN) else mode

    async def _watch_system_theme(self):
        """Linux: follow the desktop's dark/light switch without blocking the window (the check runs a program)."""
        while not self._closing:
            await asyncio.sleep(15)
            if self.s["appearance"] != "System":
                continue
            theme = await asyncio.to_thread(system_theme)
            if theme != self._sys_theme and self.s["appearance"] == "System":
                self._sys_theme = theme
                self.ui(ctk.set_appearance_mode, theme)

    def _init_fonts(self):
        fams = set(tkfont.families(self))
        if IS_WIN:
            body = ("Segoe UI Variable Text", "Segoe UI")
            head = ("Segoe UI Variable Display", "Segoe UI")
            mono = ("Cascadia Mono", "Consolas")
            fallback = "Arial"
        else:
            body = head = ("Inter", "Noto Sans", "Ubuntu", "Cantarell", "Adwaita Sans", "Open Sans", "Roboto",
                           "DejaVu Sans", "Liberation Sans", "Helvetica Neue", "Helvetica")
            mono = ("JetBrains Mono", "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono", "Ubuntu Mono", "Menlo")
            fallback = "sans-serif" if IS_LINUX else "Helvetica"
        self.body_font = next((f for f in body if f in fams), fallback)
        self.head_font = next((f for f in head if f in fams), self.body_font)
        self.mono_font = next((f for f in mono if f in fams), "monospace" if IS_LINUX else "Courier")
        self.icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        # make every widget default to the system UI font instead of customtkinter's Roboto/Arial
        key = "Windows" if IS_WIN else "macOS" if sys.platform == "darwin" else "Linux"
        ctk.ThemeManager.theme["CTkFont"][key] = {"family": self.body_font, "size": 13, "weight": "normal"}

    def F(self, size=13, bold=False, head=False):
        return ctk.CTkFont(family=self.head_font if head else self.body_font, size=size,
                           weight="bold" if bold else "normal")

    def I(self, size=18):
        return ctk.CTkFont(family=self.icon_font or self.body_font, size=size)

    def icon(self, glyph, fallback):
        return glyph if self.icon_font else fallback

    def _icon_image(self, glyph, px, color):
        """Drawn replacement for an icon-font glyph. `color` may be a (light, dark) pair like any widget colour."""
        key = (glyph, px, str(color))
        img = self._icon_cache.get(key)
        if img is None:
            light, dark = (color, color) if isinstance(color, str) else color
            fix = lambda c: "#ffffff" if c == "white" else c
            name = ICON_NAMES[glyph]
            img = ctk.CTkImage(light_image=icons.draw(name, px * 2, fix(light)),
                               dark_image=icons.draw(name, px * 2, fix(dark)), size=(px, px))
            self._icon_cache[key] = img
        return img

    def set_icon(self, btn, glyph, fallback, color=None):
        """Show an icon on a button: the icon-font glyph where the font exists (Windows), a drawn image elsewhere."""
        btn.icon_name = ICON_NAMES.get(glyph, fallback)
        color = color if color is not None else btn.cget("text_color")
        if self.icon_font or glyph not in ICON_NAMES:
            self._put(btn, text=self.icon(glyph, fallback), text_color=color)
            return
        key = (glyph, str(color))
        if getattr(btn, "_icon_key", None) != key:
            btn._icon_key = key
            btn.configure(image=self._icon_image(glyph, btn.icon_px, color), text="", text_color=color)

    @staticmethod
    def _put(widget, **kw):
        """configure() only what actually differs. The speaker is polled every few seconds and nearly always reports
        what is already on screen; skipping the no-op updates avoids redrawing the widgets each time."""
        changed = {}
        for k, v in kw.items():
            try:
                if widget.cget(k) != v:
                    changed[k] = v
            except Exception:
                changed[k] = v
        if changed:
            widget.configure(**changed)

    # ------------------------------------------------------------ window helpers
    def _apply_icon(self, win):
        try:
            if not win.winfo_exists():
                return
            if IS_WIN:
                win.iconbitmap(str(ICON))
                return
            if self._icon_photo is None:
                from PIL import ImageTk
                img = _PILImage.open(ICON)
                img.size = max(img.info.get("sizes", [img.size]))  # the largest picture in the .ico
                self._icon_photo = ImageTk.PhotoImage(img.convert("RGBA").resize((128, 128), _PILImage.LANCZOS))
            win.iconphoto(False, self._icon_photo)
        except Exception as e:  # a missing icon must never stop the app
            logging.info("window icon not set: %s", e)

    def _set_icon(self):
        self._apply_icon(self)
        # customtkinter resets the icon shortly after creation
        self._timers["icon"] = self.after(300, lambda: self._apply_icon(self))

    def _on_map_change(self, event):
        if event.widget is not self:
            return
        visible = event.type == tk.EventType.Map
        was, self._visible = self._visible, visible
        if visible and not was and self.speaker:
            self._schedule_refresh()  # catch up at once; polling was slowed down while hidden

    def _gc_tick(self):
        self._gc_runs += 1
        gc.collect(2 if self._gc_runs % 12 == 0 else 1)  # young objects every 5 s, everything once a minute
        self._every("gc", 5000, self._gc_tick)

    def _every(self, name, ms, fn):
        """Schedule the next run of a repeating timer, remembering its id so quit_app() can cancel it."""
        if self._closing:
            return
        try:
            self._timers[name] = self.after(ms, fn)
        except tk.TclError:
            pass  # window is gone

    def _restore_position(self):
        pos = self.s.get("window_pos", "")
        if self.s["remember_position"] and pos:
            try:
                x, y = (int(v) for v in pos.split(","))
                if -200 < x < self.winfo_screenwidth() - 100 and -50 < y < self.winfo_screenheight() - 100:
                    self.geometry(f"+{x}+{y}")
            except ValueError:
                pass

    def set(self, key, value):
        self.s[key] = value
        self.cfg["settings"] = self.s
        save_config(self.cfg)

    def display_name(self):
        return self.s["device_name"].strip() or self.speaker_name or APP_NAME

    def show_window(self):
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _minimize_on_start(self):
        if not self.s["start_minimized"]:
            return
        if self.s["close_action"] == "Minimize to tray":
            self._hide_to_tray()
        else:
            self.iconify()

    def _hide_to_tray(self):
        """Hide to the tray; if this desktop has no tray (or it fails), minimize instead so the window can't get lost."""
        tray = self.tray
        try:
            if not tray_available():
                raise RuntimeError("this desktop has no system tray")
            if tray is None:
                import pystray
                from PIL import Image
                menu = pystray.Menu(
                    pystray.MenuItem("Show", lambda: self.ui(self.show_window), default=True),
                    pystray.MenuItem("Quit", lambda: self.ui(self.quit_app)),
                )
                tray = pystray.Icon("bose", Image.open(ICON), self.display_name(), menu)
                tray.run_detached()
        except Exception as e:
            logging.info("tray unavailable, minimizing instead: %s", e)
            self.iconify()
            return
        self.tray = tray
        self.withdraw()

    def _on_close(self):
        action = self.s["close_action"]
        if action == "Exit":
            self.quit_app()
        elif action == "Minimize to tray":
            self._hide_to_tray()
        else:
            self.iconify()

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        if self.s["remember_position"]:
            try:
                self.set("window_pos", f"{self.winfo_x()},{self.winfo_y()}")
            except tk.TclError:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        try:  # close the speaker connection and stop the background work, but never let that hold up quitting
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout=2)
        except Exception:
            pass
        if self._instance_sock:
            for close in (lambda: self._instance_sock.shutdown(socket.SHUT_RDWR),  # wakes the listener thread
                          self._instance_sock.close):                              # frees the port straight away
                try:
                    close()
                except OSError:
                    pass
        for job in (*self._timers.values(), self._art_job, self._seek_job, self._volume_job):
            if job:
                try:
                    self.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
        self.destroy()
        gc.collect()  # free the window's Tk objects here, on the Tk thread (see __init__)
        gc.enable()

    async def _shutdown(self):
        self._conn_gen += 1
        speaker, self.speaker = self.speaker, None
        if speaker:
            try:
                await asyncio.wait_for(speaker.disconnect(), 1.5)
            except Exception:
                pass
        me = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not me:
                task.cancel()

    def _instance_listener(self, srv):
        srv.settimeout(1.0)  # wake up regularly so the thread ends when the app does
        while not self._closing:
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(2.0)  # a client that connects and says nothing must not block later launches
                    if conn.recv(16) == b"show":
                        self.ui(self.show_window)
            except OSError:
                continue

    # ------------------------------------------------------------ layout
    def _build_all(self):
        saved_errors = self.err_text.get("1.0", "end").strip() if hasattr(self, "err_text") else ""
        for child in self.winfo_children():
            child.destroy()
        self._prog_shown = False
        self._icon_cache.clear()
        self._build_header()
        self._build_login()
        self._build_remote()
        self._build_settings()
        self._build_error_box()
        if saved_errors:
            self._append_error(saved_errors)
        self._view = None
        self._show(self._main_view)
        self.set_status(*self._status)
        if self._last:
            self._apply(*self._last)
        self._render_sources(force=True)
        if self.update_info:
            self._show_update_bar()

    def _btn(self, parent, text, command, **kw):
        base = dict(height=38, corner_radius=12, fg_color=BTN, hover_color=HOVER,
                    text_color=TEXT, font=self.F(13))
        base.update(kw)
        return Btn(parent, text=text, command=command, **base)

    def _primary(self, parent, text, command, **kw):
        base = dict(height=38, corner_radius=12, fg_color=self.accent, hover_color=self.accent_hover,
                    text_color="white", font=self.F(13, bold=True))
        base.update(kw)
        return Btn(parent, text=text, command=command, **base)

    def _icon_btn(self, parent, glyph, fallback, command, size=40, **kw):
        base = dict(width=size, height=size, corner_radius=size // 2, fg_color="transparent",
                    hover_color=HOVER, text_color=MUTED, font=self.I(int(size * 0.45)))
        base.update(kw)
        btn = Btn(parent, text="", command=command, **base)
        btn.icon_px = int(size * 0.5)
        self.set_icon(btn, glyph, fallback)
        return btn

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(20, 0))
        self.head = head
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self.title_main = ctk.CTkLabel(left, text=self.display_name(), anchor="w", font=self.F(26, True, head=True))
        self.title_main.pack(fill="x")
        srow = ctk.CTkFrame(left, fg_color="transparent")
        srow.pack(fill="x", pady=(2, 0))
        self.dot = ctk.CTkLabel(srow, text="●", text_color=MUTED, font=self.F(11), width=14)
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(srow, text="Starting...", text_color=MUTED, anchor="w", font=self.F(12),
                                   wraplength=300, justify="left")
        self.status.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._icon_btn(head, "", "⚙", self._toggle_settings).pack(side="right", padx=(2, 0))
        self.power_btn = self._icon_btn(head, "", "⏻", lambda: self.cmd("set_power_state", not self.power_on))
        self.power_btn.pack(side="right", padx=2)
        self.title(self.display_name())

        self.update_bar = ctk.CTkFrame(self, fg_color=self.accent, corner_radius=14)
        self.update_lbl_bar = ctk.CTkLabel(self.update_bar, text="", text_color="white", font=self.F(12, True))
        self.update_lbl_bar.pack(side="left", padx=(14, 6), pady=8)
        self.update_btn = ctk.CTkButton(self.update_bar, text="Install", width=90, height=28, corner_radius=14,
                                        fg_color="white", hover_color="#e5e7eb", text_color="#111827",
                                        font=self.F(12, True), command=self._install_update)
        self.update_btn.pack(side="right", padx=8, pady=6)

    def _build_login(self):
        self.login_frame = ctk.CTkFrame(self, fg_color=CARD, corner_radius=24)
        ctk.CTkLabel(self.login_frame, text="Sign in to Bose", font=self.F(20, True, head=True)
                     ).pack(anchor="w", padx=28, pady=(28, 2))
        ctk.CTkLabel(self.login_frame, text="Your password is never stored on this PC.", text_color=MUTED,
                     font=self.F(12)).pack(anchor="w", padx=28, pady=(0, 16))
        entry = dict(height=44, corner_radius=12, border_width=0, fg_color=BTN, font=self.F(13))
        self.email = ctk.CTkEntry(self.login_frame, placeholder_text="Email", **entry)
        self.email.pack(fill="x", padx=28, pady=5)
        self.password = ctk.CTkEntry(self.login_frame, placeholder_text="Password", show="•", **entry)
        self.password.pack(fill="x", padx=28, pady=5)
        self.show_pw = ctk.CTkCheckBox(self.login_frame, text="Show password", command=self._toggle_pw,
                                       fg_color=self.accent, hover_color=self.accent_hover, font=self.F(12),
                                       corner_radius=6, border_width=2)
        self.show_pw.pack(anchor="w", padx=28, pady=10)
        self.login_btn = self._primary(self.login_frame, "Log in", self._do_login, height=46, corner_radius=14)
        self.login_btn.pack(fill="x", padx=28, pady=(6, 28))
        self.password.bind("<Return>", lambda _e: self._do_login())
        self.email.bind("<Return>", lambda _e: self.password.focus())
        self.email.insert(0, self.cfg.get("email", ""))

    def _build_remote(self):
        self.remote = ctk.CTkFrame(self, fg_color=CARD, corner_radius=24)

        self.art_default_pil = make_art_pil(self.accent)
        self.art_default = to_ctk(self.art_default_pil)
        self._art_url = None  # widget is new; force the cover to be re-applied
        self._art_pil = None  # ...and don't fade from the old widget's picture
        self.art = ctk.CTkLabel(self.remote, text="♪", image=self.art_default, compound="center",
                                font=self.F(60), text_color="white")
        self.art.pack(pady=(28, 16))
        self.title_lbl = ctk.CTkLabel(self.remote, text="Nothing playing", wraplength=380,
                                      font=self.F(20, True, head=True))
        self.title_lbl.pack(padx=20)
        self.sub_lbl = ctk.CTkLabel(self.remote, text="", text_color=MUTED, wraplength=380, font=self.F(13))
        self.sub_lbl.pack(padx=20, pady=(2, 0))
        self.src_badge = ctk.CTkLabel(self.remote, text="", text_color=self.accent, font=self.F(12, True))
        self.src_badge.pack(pady=(6, 8))

        # elapsed / progress bar / total (hidden when the source has no duration, e.g. TV or radio)
        self.prog = ctk.CTkFrame(self.remote, fg_color="transparent", height=24)
        self.prog.pack(fill="x", padx=28, pady=(0, 8))
        self.prog.grid_propagate(False)
        self.prog.columnconfigure(1, weight=1)
        self.pos_lbl = ctk.CTkLabel(self.prog, text="0:00", width=44, anchor="w", text_color=MUTED, font=self.F(11))
        self.pos_slider = ctk.CTkSlider(self.prog, from_=0, to=1, height=12, command=self._on_seek,
                                        progress_color=self.accent, button_color=self.accent,
                                        button_hover_color=self.accent_hover, fg_color=BTN)
        self.pos_slider.set(0)
        self.dur_lbl = ctk.CTkLabel(self.prog, text="0:00", width=44, anchor="e", text_color=MUTED, font=self.F(11))
        self.pos_lbl.grid(row=0, column=0, sticky="w")
        self.pos_slider.grid(row=0, column=1, sticky="ew", padx=6)
        self.dur_lbl.grid(row=0, column=2, sticky="e")
        self.prog.rowconfigure(0, weight=1)
        for w in (self.pos_lbl, self.pos_slider, self.dur_lbl):
            w.grid_remove()

        row = ctk.CTkFrame(self.remote, fg_color="transparent")
        row.pack(pady=4)
        nav = dict(size=56, fg_color=BTN, hover_color=HOVER, text_color=TEXT)
        self._icon_btn(row, "", "⏮", lambda: self.cmd("skip_previous"), **nav).grid(row=0, column=0, padx=10)
        self.play_btn = self._primary(row, "", self._toggle_play, width=76, height=76,
                                      corner_radius=38, font=self.I(28))
        self.play_btn.icon_px = 30
        self.set_icon(self.play_btn, "", "▶")
        self.play_btn.grid(row=0, column=1, padx=10)
        self._icon_btn(row, "", "⏭", lambda: self.cmd("skip_next"), **nav).grid(row=0, column=2, padx=10)

        vol = ctk.CTkFrame(self.remote, fg_color=BTN, corner_radius=18)
        vol.pack(fill="x", padx=24, pady=(22, 0))
        self.mute_btn = self._icon_btn(vol, "", "🔊", self._toggle_mute, size=40, text_color=TEXT)
        self.mute_btn.pack(side="left", padx=(8, 0), pady=8)
        mx = int(self.s["max_volume"])
        self.slider = ctk.CTkSlider(vol, from_=0, to=mx, number_of_steps=max(mx, 1), command=self._on_slider,
                                    progress_color=self.accent, button_color=self.accent,
                                    button_hover_color=self.accent_hover, fg_color=HOVER, height=18)
        self.slider.set(min(self.cur_vol, mx))
        self.slider.pack(side="left", fill="x", expand=True, padx=12)
        self.vol_label = ctk.CTkLabel(vol, text="-", width=44, font=self.F(15, True))
        self.vol_label.pack(side="right", padx=(0, 14))

        ctk.CTkLabel(self.remote, text="SOURCE", text_color=MUTED, anchor="w", font=self.F(11, True)
                     ).pack(fill="x", padx=28, pady=(22, 6))
        self.chips = ctk.CTkFrame(self.remote, fg_color="transparent")
        self.chips.pack(fill="x", padx=22, pady=(0, 24))

    @staticmethod
    def _source_key(c):
        return f"{c[1]}|{c[2] or ''}"

    def _apply_source_filter(self):
        hidden = set(self.s.get("hidden_sources", []))
        self.sources = [c for c in self.all_sources if self._source_key(c) not in hidden]

    def _render_source_settings(self):
        for w in self.src_list.winfo_children():
            w.destroy()
        if not self.all_sources:
            ctk.CTkLabel(self.src_list, text="Connect to the soundbar to list its sources.", text_color=MUTED,
                         font=self.F(12), anchor="w").pack(fill="x", padx=18, pady=(10, 0))
            return
        hidden = set(self.s.get("hidden_sources", []))
        for c in self.all_sources:
            key = self._source_key(c)
            self._switch_row(self.src_list, c[0], key not in hidden,
                             lambda v, k=key: self._toggle_source(k, v))

    def _toggle_source(self, key, visible):
        hidden = set(self.s.get("hidden_sources", []))
        if visible:
            hidden.discard(key)
        else:
            hidden.add(key)
        self.set("hidden_sources", sorted(hidden))
        self._apply_source_filter()
        self._render_sources(force=True)

    async def _load_art(self, url):
        try:
            img = self._art_cache.get(url)
            if img is None:
                img = art_pil_from_bytes(await asyncio.to_thread(fetch_image, url))
                if len(self._art_cache) > 12:
                    self._art_cache.pop(next(iter(self._art_cache)))
                self._art_cache[url] = img
        except Exception as e:
            logging.info("album art unavailable: %s", e)
            return
        if url == self._art_url:  # still the current track
            self.ui(lambda: self._set_art(img))

    def _render_sources(self, force=False):
        key = (tuple(self.sources), self.cur_source, self.accent, tuple(self._picked.items()))
        if not force and key == getattr(self, "_chips_key", None):
            return
        self._chips_key = key
        for w in self.chips.winfo_children():
            w.destroy()
        for c in range(2):
            self.chips.columnconfigure(c, weight=1, uniform="chip")
        if not self.sources:
            ctk.CTkLabel(self.chips, text="Connect to a soundbar to see its sources.", text_color=MUTED,
                         font=self.F(12)).grid(row=0, column=0, columnspan=2, pady=6)
            return
        # several chips can share a source (two Spotify accounts): light up only one of them
        same = [i for i, c in enumerate(self.sources) if c[1] == self.cur_source]
        picked = self._picked.get(self.cur_source)
        active_i = next((i for i in same if self.sources[i][2] == picked), same[0] if same else None)
        for i, (label, source, account) in enumerate(self.sources):
            active = i == active_i
            Btn(
                self.chips, text=label, height=42, corner_radius=21, font=self.F(13, active),
                fg_color=self.accent if active else BTN, hover_color=self.accent_hover if active else HOVER,
                text_color="white" if active else TEXT,
                command=lambda s=source, a=account: self._pick_source(s, a),
            ).grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="ew")

    # ---- settings page
    def _section(self, text):
        ctk.CTkLabel(self.settings, text=text.upper(), text_color=MUTED, anchor="w",
                     font=self.F(11, True)).pack(fill="x", padx=8, pady=(18, 6))
        card = ctk.CTkFrame(self.settings, fg_color=CARD, corner_radius=18)
        card.pack(fill="x")
        return card

    def _end_card(self, card):
        ctk.CTkFrame(card, fg_color="transparent", height=12).pack()

    def _row(self, card, label, hint=None):
        """A label on the left with an optional hint underneath; returns the frame, put the control in column 1."""
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(12, 0))
        row.columnconfigure(0, weight=1)
        ctk.CTkLabel(row, text=label, anchor="w", font=self.F(13)).grid(row=0, column=0, sticky="w")
        if hint:
            ctk.CTkLabel(row, text=hint, text_color=MUTED, font=self.F(11), anchor="w", justify="left",
                         wraplength=290).grid(row=1, column=0, sticky="w")
        return row

    def _switch_row(self, card, label, value, command, hint=None):
        row = self._row(card, label, hint)
        sw = ctk.CTkSwitch(row, text="", width=46, progress_color=self.accent,
                           command=lambda: command(bool(sw.get())))
        sw.grid(row=0, column=1, rowspan=2, sticky="e")
        if value:
            sw.select()
        return sw

    def _option_row(self, card, label, values, current, command, width=190):
        row = self._row(card, label)
        menu = ctk.CTkOptionMenu(row, values=values, command=command, width=width, height=34, corner_radius=10,
                                 fg_color=BTN, button_color=BTN, button_hover_color=HOVER, text_color=TEXT,
                                 dropdown_fg_color=CARD, dropdown_text_color=TEXT, dropdown_hover_color=HOVER,
                                 font=self.F(13), dropdown_font=self.F(13))
        menu.set(current)
        menu.grid(row=0, column=1, sticky="e")
        return menu

    def _build_settings(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        self.settings_outer = outer
        bar = ctk.CTkFrame(outer, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 4))
        self._icon_btn(bar, "", "←", self._toggle_settings, size=40, text_color=TEXT).pack(side="left")
        ctk.CTkLabel(bar, text="Settings", font=self.F(20, True, head=True)).pack(side="left", padx=8)
        self.settings = ctk.CTkScrollableFrame(outer, fg_color="transparent", corner_radius=0)
        self.settings.pack(fill="both", expand=True)

        card = self._section("Device")
        row = self._row(card, "Display name",
                        "Label shown in this app only; it doesn't rename the speaker. Empty = use the speaker's name.")
        self.name_entry = ctk.CTkEntry(card, height=40, corner_radius=10, border_width=0, fg_color=BTN,
                                       font=self.F(13), placeholder_text=self.speaker_name or "Display name")
        self.name_entry.pack(fill="x", padx=18, pady=(8, 0))
        if self.s["device_name"]:
            self.name_entry.insert(0, self.s["device_name"])
        self.name_entry.bind("<Return>", lambda _e: self._save_name())
        self.name_entry.bind("<FocusOut>", lambda _e: self._save_name())
        self._end_card(card)

        card = self._section("Appearance")
        row = self._row(card, "Mode")
        seg = ctk.CTkSegmentedButton(row, values=["Dark", "Light", "System"], command=self._set_appearance,
                                     selected_color=self.accent, selected_hover_color=self.accent_hover,
                                     unselected_color=BTN, unselected_hover_color=HOVER, text_color=TEXT,
                                     fg_color=BTN, font=self.F(12), corner_radius=10)
        seg.set(self.s["appearance"])
        seg.grid(row=0, column=1, sticky="e")
        row = self._row(card, "Accent colour")
        sw = ctk.CTkFrame(row, fg_color="transparent")
        sw.grid(row=0, column=1, sticky="e")
        for name, hexc in ACCENTS.items():
            ctk.CTkButton(sw, text="", width=26, height=26, corner_radius=13, fg_color=hexc,
                          hover_color=darken(hexc), border_width=3 if name == self.s["accent"] else 0,
                          border_color=("#111827", "#ffffff"),
                          command=lambda n=name: self.after(20, lambda: self._set_accent(n))
                          ).pack(side="left", padx=2)
        self._switch_row(card, "Always on top", self.s["always_on_top"],
                         lambda v: (self.set("always_on_top", v), self.attributes("-topmost", v)))
        if IS_LINUX:  # Windows scales the window with the display by itself
            self._option_row(card, "Interface size", ["100%", "125%", "150%", "175%", "200%"],
                             f"{int(self.s.get('ui_scale') or 100)}%", self._set_scale, width=110)
        self._end_card(card)

        card = self._section("Window")
        self._option_row(card, "When I close the window", CLOSE_ACTIONS, self.s["close_action"],
                         lambda v: self.set("close_action", v))
        self._switch_row(card, "Start minimized at sign-in", self.s["start_minimized"],
                         lambda v: self.set("start_minimized", v))
        self._switch_row(card, "Remember window position", self.s["remember_position"],
                         lambda v: self.set("remember_position", v))
        self._switch_row(card, "Keyboard shortcuts", self.s["keyboard_shortcuts"],
                         lambda v: self.set("keyboard_shortcuts", v),
                         "Space play/pause · ↑↓ volume · ←→ previous/next · M mute")
        self._end_card(card)

        card = self._section("Windows integration" if IS_WIN else "Desktop integration")
        self._switch_row(card, "Desktop shortcut", shortcut_exists("desktop"),
                         lambda v: self._toggle_shortcut("desktop", v))
        self._switch_row(card, "Start menu entry" if IS_WIN else "Applications menu entry", shortcut_exists("start"),
                         lambda v: self._toggle_shortcut("start", v))
        self._switch_row(card, "Run when I sign in to Windows" if IS_WIN else "Run when I sign in",
                         shortcut_exists("startup"), lambda v: self._toggle_shortcut("startup", v))
        if IS_WIN:
            row = self._row(card, "Pin to taskbar", "Windows doesn't let apps pin themselves.")
            self._btn(row, "How?", self._pin_help, width=70, height=32).grid(row=0, column=1, rowspan=2, sticky="e")
        self._end_card(card)

        card = self._section("Sound")
        row = self._row(card, f"Maximum volume: {self.s['max_volume']}")
        self.max_lbl = row.winfo_children()[0]
        ms = ctk.CTkSlider(card, from_=10, to=100, number_of_steps=90, progress_color=self.accent,
                           button_color=self.accent, button_hover_color=self.accent_hover, fg_color=BTN,
                           command=self._set_max_volume)
        ms.set(self.s["max_volume"])
        ms.pack(fill="x", padx=18, pady=(8, 0))
        self._option_row(card, "Keyboard volume step", ["1", "2", "5", "10"], str(self.s["volume_step"]),
                         lambda v: self.set("volume_step", int(v)), width=90)
        self._end_card(card)

        card = self._section("Connection")
        self._row(card, "Speaker IP address", "Leave empty to auto-detect.")
        iprow = ctk.CTkFrame(card, fg_color="transparent")
        iprow.pack(fill="x", padx=18, pady=(8, 0))
        self.ip_entry = ctk.CTkEntry(iprow, height=38, corner_radius=10, border_width=0, fg_color=BTN,
                                     font=self.F(13), placeholder_text="192.168.1.50")
        self.ip_entry.pack(side="left", fill="x", expand=True)
        if self.cfg.get("host"):
            self.ip_entry.insert(0, self.cfg["host"])
        self._primary(iprow, "Save & connect", self._save_ip, width=120, height=38).pack(side="left", padx=(8, 0))
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(10, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="c")
        self._btn(grid, "Refresh", lambda: self.run(self._refresh(sources=True))).grid(row=0, column=0, padx=(0, 4), pady=3, sticky="ew")
        self._btn(grid, "Reconnect", self._reconnect).grid(row=0, column=1, padx=(4, 0), pady=3, sticky="ew")
        self._btn(grid, "Re-scan network", self._forget_speaker).grid(row=1, column=0, padx=(0, 4), pady=3, sticky="ew")
        self._btn(grid, "Log out", self._logout).grid(row=1, column=1, padx=(4, 0), pady=3, sticky="ew")
        self._end_card(card)

        card = self._section("Sources on the main screen")
        ctk.CTkLabel(card, text="Turn off any input you don't want to see. The speaker remembers every Spotify "
                     "account that was ever linked to it, so you may see ones you don't recognise.",
                     text_color=MUTED, anchor="w", justify="left", wraplength=380, font=self.F(11)
                     ).pack(fill="x", padx=18, pady=(12, 0))
        self.src_list = ctk.CTkFrame(card, fg_color="transparent")
        self.src_list.pack(fill="x")
        self._end_card(card)
        self._render_source_settings()

        card = self._section("Advanced")
        self._switch_row(card, "Show error messages", self.s["show_log"], self._set_error_log,
                         "Shows a panel with any errors at the bottom of the window.")
        self._switch_row(card, "Check for updates at startup", self.s["check_updates"],
                         lambda v: self.set("check_updates", v),
                         "Contacts GitHub only to ask for the latest version number.")
        row = self._row(card, "Updates", f"Installed: v{APP_VERSION}")
        self.update_lbl = row.winfo_children()[1] if len(row.winfo_children()) > 1 else row.winfo_children()[0]
        self._btn(row, "Check now", lambda: self.run(self._check_updates(manual=True)), width=110, height=32
                  ).grid(row=0, column=1, rowspan=2, sticky="e")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(10, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="c")
        self._btn(grid, "Open data folder", self._open_data_folder).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self._btn(grid, "Reset settings", self._reset_settings).grid(row=0, column=1, padx=(4, 0), sticky="ew")
        self._end_card(card)

        card = self._section("About")
        ctk.CTkLabel(card, text=f"{APP_NAME} Remote  ·  v{APP_VERSION}", anchor="w", font=self.F(13, True)
                     ).pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(card, text="An unofficial remote for Bose speakers. Not affiliated with Bose.",
                     text_color=MUTED, anchor="w", justify="left", wraplength=380, font=self.F(11)
                     ).pack(fill="x", padx=18)
        self._btn(card, PROJECT_URL.replace("https://", ""), lambda: webbrowser.open(PROJECT_URL)
                  ).pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(card, text="CREDITS", text_color=MUTED, anchor="w", font=self.F(11, True)
                     ).pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(card, text="Speaker control is done by pybose, an open-source library by cavefire (GPL-3.0).",
                     text_color=MUTED, anchor="w", justify="left", wraplength=380, font=self.F(11)
                     ).pack(fill="x", padx=18)
        self._btn(card, "pybose by cavefire", lambda: webbrowser.open(PYBOSE_URL)
                  ).pack(fill="x", padx=18, pady=(10, 0))
        self._end_card(card)

    def _build_error_box(self):
        self.err_frame = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        top = ctk.CTkFrame(self.err_frame, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(top, text="Error messages", text_color=RED, font=self.F(12, True)).pack(side="left")
        self._btn(top, "Clear", self._clear_errors, width=54, height=24, corner_radius=8, font=self.F(11)).pack(side="right")
        self._btn(top, "Copy", self._copy_errors, width=54, height=24, corner_radius=8, font=self.F(11)).pack(side="right", padx=6)
        self.err_text = ctk.CTkTextbox(self.err_frame, height=110, fg_color=BG, text_color=RED, corner_radius=10,
                                       font=ctk.CTkFont(family=self.mono_font, size=11), wrap="word")
        self.err_text.pack(fill="both", expand=True, padx=14, pady=10)
        self.err_text.configure(state="disabled")
        if self.s["show_log"]:
            self.err_frame.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

    def _show(self, which):
        if self._view == which:
            return
        for f in (self.login_frame, self.remote, self.settings_outer):
            f.pack_forget()
        frame = {"login": self.login_frame, "remote": self.remote, "settings": self.settings_outer}[which]
        opts = dict(fill="both", expand=True, padx=20, pady=16)
        if self.err_frame.winfo_manager():
            opts["before"] = self.err_frame
        frame.pack(**opts)
        self._view = which

    def _set_main(self, which):
        self._main_view = which
        if self._view != "settings":
            self._show(which)

    def _toggle_settings(self):
        self._show(self._main_view if self._view == "settings" else "settings")

    # ------------------------------------------------------- settings handlers
    def _save_name(self):
        name = self.name_entry.get().strip()
        if name != self.s["device_name"]:
            self.set("device_name", name)
            self.title_main.configure(text=self.display_name())
            self.title(self.display_name())

    def _set_appearance(self, v):
        self.set("appearance", v)
        if v == "System" and not IS_WIN:
            self._sys_theme = system_theme()
        ctk.set_appearance_mode(self._appearance())

    def _set_scale(self, v):
        pct = int(str(v).rstrip("%"))
        self.set("ui_scale", pct)
        set_ui_scale(pct)

    def _open_data_folder(self):
        try:
            open_path(DATA_DIR)
        except Exception as e:
            self.log_error(f"Could not open {DATA_DIR}", e)

    def _set_accent(self, v):
        self.set("accent", v)
        self.accent = ACCENTS[v]
        self.accent_hover = darken(self.accent)
        view = self._view
        self._build_all()
        self._view = None
        self._show(view)

    def _set_error_log(self, v):
        self.set("show_log", v)
        view, self._view = self._view, None
        if v:
            self.err_frame.pack(side="bottom", fill="x", padx=20, pady=(0, 16))
        else:
            self.err_frame.pack_forget()
        self._show(view)

    def _toggle_shortcut(self, kind, enabled):
        try:
            create_shortcut(kind) if enabled else remove_shortcut(kind)
            self.set_status(f"{kind.capitalize()} shortcut {'added' if enabled else 'removed'}", *self._status[1:])
        except Exception as e:
            self.log_error(f"Could not update the {kind} shortcut", e)

    def _pin_help(self):
        win = ctk.CTkToplevel(self, fg_color=BG)
        win.title("Pin to taskbar")
        win.geometry("380x230")
        win.transient(self)
        win.after(300, lambda: self._apply_icon(win))
        ctk.CTkLabel(win, justify="left", wraplength=330, font=self.F(13), text=(
            "1.  Keep the app open.\n"
            "2.  Right-click its icon in the taskbar.\n"
            "3.  Choose “Pin to taskbar”.\n\n"
            "Windows 11 removed the ability for apps to pin themselves, so this has to be done by hand once."
        )).pack(padx=24, pady=24)
        self._primary(win, "Got it", win.destroy, width=120).pack(pady=(0, 20))

    def _set_max_volume(self, v):
        v = int(v)
        self.max_lbl.configure(text=f"Maximum volume: {v}")
        self.set("max_volume", v)
        self.slider.configure(to=v, number_of_steps=v)
        self.slider.set(min(self.cur_vol, v))
        if self.speaker and self.cur_vol > v:
            self.cmd("set_audio_volume", v)

    def _save_ip(self):
        ip = self.ip_entry.get().strip()
        if ip:
            self.cfg["host"] = ip
        else:
            self.cfg.pop("host", None)
        self.cfg.pop("guid", None)
        save_config(self.cfg)
        self._reconnect()

    def _forget_speaker(self):
        self.cfg.pop("host", None)
        self.cfg.pop("guid", None)
        save_config(self.cfg)
        self.ip_entry.delete(0, "end")
        self._reconnect()

    def _reset_settings(self):
        self.s = dict(DEFAULTS)
        self.cfg["settings"] = self.s
        save_config(self.cfg)
        ctk.set_appearance_mode(self._appearance())
        if IS_LINUX:
            set_ui_scale(100)
        self.accent = ACCENTS[self.s["accent"]]
        self.accent_hover = darken(self.accent)
        self.attributes("-topmost", False)
        self._build_all()
        self._view = None
        self._show("settings")

    # --------------------------------------------------- keyboard shortcuts
    def _bind_keys(self):
        def guard(fn):
            def handler(_e):
                if not self.s["keyboard_shortcuts"] or self._view != "remote":
                    return None
                if isinstance(self.focus_get(), (tk.Entry, tk.Text)):
                    return None
                fn()
                return "break"
            return handler
        step = lambda: int(self.s["volume_step"])
        self.bind("<space>", guard(self._toggle_play))
        self.bind("<Up>", guard(lambda: self._nudge_volume(step())))
        self.bind("<Down>", guard(lambda: self._nudge_volume(-step())))
        self.bind("<Left>", guard(lambda: self.cmd("skip_previous")))
        self.bind("<Right>", guard(lambda: self.cmd("skip_next")))
        self.bind("<m>", guard(self._toggle_mute))

    def _nudge_volume(self, delta):
        v = max(0, min(int(self.s["max_volume"]), self.cur_vol + delta))
        self.slider.set(v)
        self._on_slider(v)

    # --------------------------------------------------- logging / errors
    def ui(self, fn, *a):
        """Run fn(*a) on the Tk thread. Safe to call from any thread: Tk itself must only be touched from its own
        thread, so work is queued and picked up by _drain_ui."""
        self._ui_queue.put((fn, a))

    def _drain_ui(self):
        try:
            while True:
                fn, a = self._ui_queue.get_nowait()
                try:
                    fn(*a)
                except tk.TclError:
                    pass  # widget was destroyed while the call was queued
                except Exception:
                    logging.exception("UI callback failed")
        except queue.Empty:
            pass
        # 30 ms keeps the window responsive; while it is minimized or in the tray nobody is watching, so the
        # process is allowed to sleep far longer between checks
        self._every("drain", 30 if self._visible else 200, self._drain_ui)

    def set_status(self, text, color=MUTED, dot=None):
        self._status = (text, color, dot or self._status[2])

        def go():
            self._put(self.status, text=text, text_color=color)
            if dot:
                self._put(self.dot, text_color=dot)
        self.ui(go)

    def log_error(self, context, exc=None):
        detail = "".join(traceback.format_exception(exc)) if exc else ""
        logging.error("%s %s", context, detail)
        msg = f"{context}\n{type(exc).__name__}: {exc}\n" if exc else f"{context}\n"
        self.ui(self._append_error, msg)
        self.set_status(context, RED, RED)

    def _append_error(self, msg):
        self.err_text.configure(state="normal")
        self.err_text.insert("end", msg + "\n")
        lines = int(self.err_text.index("end-1c").split(".")[0])
        if lines > MAX_ERROR_LINES:  # an error that repeats for days must not grow this without limit
            self.err_text.delete("1.0", f"{lines - MAX_ERROR_LINES + 1}.0")
        self.err_text.see("end")
        self.err_text.configure(state="disabled")

    def _tk_exception(self, exc_type, exc, tb):
        self.log_error("UI error", exc)

    def _clear_errors(self):
        self.err_text.configure(state="normal")
        self.err_text.delete("1.0", "end")
        self.err_text.configure(state="disabled")

    def _copy_errors(self):
        self.clipboard_clear()
        self.clipboard_append(self.err_text.get("1.0", "end"))

    # ------------------------------------------------------------ async
    def run(self, coro, context="Operation failed"):
        async def wrapped():
            try:
                await coro
            except Exception as e:
                if not self._closing:
                    self.log_error(context, e)
        try:
            asyncio.run_coroutine_threadsafe(wrapped(), self.loop)
        except RuntimeError:  # the loop has been shut down
            coro.close()

    def cmd(self, name, *args):
        speaker = self.speaker
        if not speaker:
            self.log_error("Not connected to a soundbar yet")
            return

        async def go():
            await self._call(speaker, name, *args)
            await self._refresh()
        self.run(go(), f"Command '{name}' failed")

    async def _call(self, speaker, name, *args):
        """Run one speaker command. If it fails because the connection dropped or the login expired, repair that
        and try once more, so a button press after the PC woke from sleep just works."""
        try:
            return await getattr(speaker, name)(*args)
        except Exception as e:
            if not await self._recover(speaker, e):
                raise
        return await getattr(speaker, name)(*args)

    @staticmethod
    def _is_auth_error(exc):
        return getattr(exc, "http_status", None) in (401, 403)

    async def _recover(self, speaker, exc, force=False):
        """Try to repair whatever made a request fail. True if something was repaired and a retry makes sense.

        - the speaker rejected our login (401/403): renew the token;
        - the request never got an answer in the usual sense (no HTTP-like status: the socket is gone): reconnect;
        - the speaker answered with an error or stayed silent (408): nothing to repair for a command, since it may
          have been carried out. The background poll passes force=True to reconnect anyway.
        """
        if self.speaker is not speaker or self._closing:
            return False
        if self._is_auth_error(exc):
            await self._renew_token(force=True)
            return self.speaker is speaker
        if getattr(exc, "http_status", None) is not None and not force:
            return False
        return await self._revive(speaker)

    async def _revive(self, speaker):
        """Re-open the connection of the current speaker. True if it is (still) the current speaker afterwards."""
        async with self._conn_lock:
            if self.speaker is not speaker or self._closing:
                return False
            await asyncio.wait_for(speaker.connect(), CONNECT_TIMEOUT)  # pybose re-subscribes by itself
        return True

    def _pick_source(self, source, account):
        speaker = self.speaker
        if not speaker:
            self.log_error("Not connected to a soundbar yet")
            return
        label = next((lbl for lbl, s, a in self.sources if s == source and a == account), source)
        self._picked[source] = account
        self._render_sources()

        async def go():
            body = {"source": source}
            if account:
                body["sourceAccount"] = account
            self.set_status(f"Switching to {label}...")
            await self._call(speaker, "_request", "/content/playbackRequest", "POST", body)
            await asyncio.sleep(0.6)
            await self._refresh()
            self.set_status(f"Connected  ·  {self.cfg.get('host', '')}", GREEN, GREEN)
        self.run(go(), f"Could not switch to {label}")

    # -------------------------------------------------------- connection
    @staticmethod
    def _is_offline_error(exc):
        """The Bose login service could not be reached (as opposed to: it refused us)."""
        return isinstance(exc, (OSError, BoseAuthUnavailableError)) and not isinstance(exc, BoseAuthRejectedError)

    async def _startup(self):
        shutil.rmtree(DATA_DIR / "update", ignore_errors=True)  # leftovers from a previous self-update
        if self.s["check_updates"] and REPO:
            self.run(self._check_updates(), "Update check failed")
        tok = self.cfg.get("token")
        if not tok:
            self.set_status("Please log in")
            self.ui(self._set_main, "login")
            return
        self.set_status("Restoring saved login...")
        delay = LOGIN_RETRY_SECONDS
        while not self._closing:
            try:
                await self._restore_token(tok)
                break
            except Exception as e:
                if self._is_offline_error(e):
                    # typical at sign-in: the app starts before the network is up. Keep the login and wait.
                    logging.info("can't reach Bose to renew the login yet: %s", e)
                    self.set_status("Waiting for an internet connection to renew the login...", MUTED, RED)
                    await asyncio.sleep(delay)
                    delay = min(MAX_RETRY_SECONDS, delay * 2)
                    continue
                self.log_error("Saved login no longer works, please log in again", e)
                self.cfg.pop("token", None)
                self.ui(self._set_main, "login")
                return
        await self._connect()

    async def _restore_token(self, tok):
        self.auth = BoseAuth()
        self.auth.set_access_token(tok["access_token"], tok["refresh_token"], tok["bose_person_id"])
        if self.cfg.get("azure_refresh_token"):
            self.auth.set_azure_refresh_token(self.cfg["azure_refresh_token"])
        if not self.auth.is_token_valid():
            await asyncio.to_thread(self.auth.do_token_refresh)
        self._save_tokens()

    def _save_tokens(self):
        t = self.auth.getCachedToken()
        if t:
            self.cfg["token"] = dict(t)
            self.cfg["azure_refresh_token"] = self.auth.get_azure_refresh_token()
            save_config(self.cfg)

    async def _renew_token(self, force=False):
        """The Bose login expires after a while and pybose does not renew it by itself. An app that sits in the tray
        for days would otherwise end up with every request rejected, forever. Called before each poll (cheap: it
        only reads the expiry time) and when the speaker rejects the token."""
        auth = self.auth
        if not force:
            remaining = getattr(auth, "get_token_validity_time", None)
            if (remaining() > 120) if remaining else auth.is_token_valid():
                return
        try:
            await asyncio.to_thread(auth.do_token_refresh)
        except Exception as e:
            if not self._is_offline_error(e) and auth is self.auth:
                self._login_expired(e)
            raise
        if auth is self.auth:
            self._save_tokens()
            logging.info("login renewed")

    def _login_expired(self, exc):
        """Bose refused to renew the login: only signing in again helps."""
        logging.warning("the saved login can't be renewed: %s", exc)
        self.cfg.pop("token", None)
        self.cfg.pop("azure_refresh_token", None)
        save_config(self.cfg)
        self._conn_gen += 1
        speaker, self.speaker = self.speaker, None
        if speaker:
            asyncio.run_coroutine_threadsafe(self._close_speaker(speaker), self.loop)
        self.set_status("Your Bose login expired, please log in again", RED, RED)
        self.ui(self._set_main, "login")

    def _toggle_pw(self):
        self.password.configure(show="" if self.show_pw.get() else "•")

    def _new_auth(self):
        """Fresh BoseAuth whose server replies to the sign-in form steps get logged (bodies hold only error text)."""
        auth = BoseAuth()

        def hook(resp, *_a, **_k):
            if "SelfAsserted" in resp.url and "confirmed" not in resp.url:
                body = resp.text.strip()[:400]
                logging.info("SelfAsserted %s -> %s", resp.status_code, body)
                if '"status":"400"' in body.replace(" ", "") or "error" in body.lower():
                    self.ui(self._append_error, f"Bose server said: {body}\n")
        auth._session.hooks["response"].append(hook)
        return auth

    def _do_login(self):
        email, pw = self.email.get().strip(), self.password.get()
        if not email or not pw:
            self.log_error("Enter both your email and password")
            return
        self.login_btn.configure(state="disabled", text="Logging in...")
        self.set_status("Logging in...")

        async def go():
            try:
                self.auth = self._new_auth()
                await asyncio.to_thread(self.auth.getControlToken, email, pw, True)
                self.cfg["email"] = email
                self._save_tokens()
            except Exception as e:
                self.log_error("Login failed", e)
                return
            finally:
                self.ui(lambda: self.login_btn.configure(state="normal", text="Log in"))
            self.ui(self.password.delete, 0, "end")
            await self._connect()
        self.run(go(), "Login failed")

    def _logout(self):
        self.run(self._disconnect())
        self.cfg.pop("token", None)
        self.cfg.pop("azure_refresh_token", None)
        save_config(self.cfg)
        self.speaker = None
        self.sources = []
        self.set_status("Logged out", MUTED, MUTED)
        self._main_view = "login"
        self.ui(self._show, "login")

    def _reconnect(self):
        self.run(self._connect(), "Reconnect failed")

    @staticmethod
    async def _close_speaker(speaker):
        try:
            await asyncio.wait_for(speaker.disconnect(), 5)
        except Exception as e:
            logging.warning("disconnect: %s", e)

    async def _disconnect(self):
        self._conn_gen += 1  # also stops any background reconnect attempts
        speaker, self.speaker = self.speaker, None
        async with self._conn_lock:
            if speaker:
                await self._close_speaker(speaker)

    async def _connect(self, quiet=False):
        """Find the speaker (unless its address is saved) and connect. `quiet` is used by the background retries:
        they don't touch the status line or the error panel, they just keep trying."""
        self._conn_gen += 1
        gen = self._conn_gen  # a newer connect/disconnect makes this attempt (and its retries) obsolete
        old, self.speaker = self.speaker, None
        async with self._conn_lock:  # never two attempts at once: that used to leave orphaned connections behind
            if old:
                await self._close_speaker(old)
            if gen != self._conn_gen or self._closing:
                return
            self.ui(self._set_main, "remote")
            host, guid = self.cfg.get("host"), self.cfg.get("guid")
            speaker = None
            try:
                if not host:
                    if not quiet:
                        self.set_status("Searching for soundbar on your network...")
                    devices = await asyncio.to_thread(BoseDiscovery().discover_devices, 5)
                    if devices:
                        host, guid = devices[0]["IP"], devices[0]["GUID"]
                    else:
                        found = []
                        # the port scan opens a connection to every address on the subnet: fine when the user is
                        # waiting for it, not something to repeat every minute in the background
                        if not quiet or time.monotonic() - self._last_scan >= SUBNET_SCAN_EVERY:
                            self._last_scan = time.monotonic()
                            if not quiet:
                                self.set_status("mDNS found nothing, scanning your network...")
                            found = await asyncio.to_thread(scan_subnet)
                        if not found:
                            if not quiet:
                                self.log_error("No Bose device found by mDNS or by scanning port 8082 on this subnet.")
                            self._retry_connect_later(gen)
                            return
                        host, guid = found[0], None  # pybose learns the device id from the first message
                    self.cfg.update(host=host, guid=guid)
                    save_config(self.cfg)
                if not quiet:
                    self.set_status(f"Connecting to {host}...")
                # auto_reconnect off: pybose's own reconnect can run twice at once and leaves failed attempts
                # unobserved; _poll() below supervises the connection instead
                speaker = BoseSpeaker(host=host, device_id=guid, bose_auth=self.auth, auto_reconnect=False)
                speaker.attach_receiver(lambda _m: self._schedule_refresh())
                await asyncio.wait_for(speaker.connect(), CONNECT_TIMEOUT)
            except Exception as e:
                if speaker:
                    await self._close_speaker(speaker)
                # a stale saved IP is a common cause; forget it so the next try rediscovers
                self.cfg.pop("host", None)
                self.cfg.pop("guid", None)
                save_config(self.cfg)
                if quiet:
                    logging.info("still can't connect to the soundbar at %s: %s", host, e)
                else:
                    self.log_error(f"Could not connect to the soundbar at {host}", e)
                self._retry_connect_later(gen)
                return
            if gen != self._conn_gen or self._closing:
                await self._close_speaker(speaker)
                return
            self.speaker = speaker
            self._retry_n = 0
        self.set_status(f"Connected  ·  {host}", GREEN, GREEN)
        try:
            await speaker.subscribe()  # ask the speaker to push now-playing / volume / source changes
        except Exception as e:
            logging.info("subscribe failed: %s", e)
        asyncio.create_task(self._poll(speaker))
        try:
            info = await speaker.get_system_info()
            self.speaker_name = info.get("name") or info.get("productName")
            logging.info("system info: %s", {k: info.get(k) for k in ("name", "productName", "productType")})
            self.ui(self._refresh_names)
        except Exception as e:
            logging.info("system info unavailable: %s", e)
        await self._refresh(sources=True)

    def _retry_connect_later(self, gen):
        """The speaker wasn't there (switched off, or the PC started before its network was up): keep trying in the
        background, less and less often, instead of staying broken until the user presses Reconnect."""
        self._retry_n += 1
        n = self._retry_n
        delay = min(MAX_RETRY_SECONDS, FIRST_RETRY_SECONDS * 2 ** min(n - 1, 3)) if n < 10 else MAX_RETRY_SECONDS * 5

        async def go():
            await asyncio.sleep(delay)
            if gen == self._conn_gen and self.speaker is None and self.cfg.get("token") and not self._closing:
                await self._connect(quiet=True)
        self.run(go(), "Reconnect failed")

    def _poll_delay(self, failures=0):
        delay = POLL_SECONDS * (1 if self._visible else HIDDEN_POLL_FACTOR)
        if failures:  # back off while the speaker is unreachable: 10 s, 20 s, 40 s, then once a minute
            delay = max(delay, min(MAX_RETRY_SECONDS, POLL_SECONDS * 2 ** min(failures, 6)))
        return delay

    async def _poll(self, speaker):
        """Safety net and supervisor for one connection: re-read the state every few seconds in case a push
        notification was missed, keep the login fresh, and bring the connection back when it drops (PC woke from
        sleep, Wi-Fi blip, speaker restarted)."""
        failures = 0
        while self.speaker is speaker and not self._closing:
            await asyncio.sleep(self._poll_delay(failures))
            if self.speaker is not speaker or self._closing:
                return
            try:
                await self._renew_token()
                if failures:  # known to be down: reconnect first instead of talking into a dead connection
                    if not await self._revive(speaker):
                        return
                    await self._refresh(sources=True)
                else:
                    try:
                        await self._refresh()
                    except Exception as e:
                        if not await self._recover(speaker, e, force=True):
                            raise
                        await self._refresh(sources=True)
            except Exception as e:
                if self.speaker is not speaker or self._closing:
                    return
                failures += 1
                logging.info("speaker not reachable: %s", e)  # repeats are collapsed by the log filter
                if failures == 1:
                    self.set_status("The speaker isn't answering, retrying...", MUTED, RED)
            else:
                if failures:
                    failures = 0
                    logging.info("speaker is reachable again")
                    self.set_status(f"Connected  ·  {self.cfg.get('host', '')}", GREEN, GREEN)

    # ------------------------------------------------------------ updates
    def _set_update_text(self, text):
        self.update_lbl.configure(text=text)

    async def _check_updates(self, manual=False):
        """Ask GitHub for the latest release; show the banner if it is newer than this build."""
        if not REPO:
            if manual:
                self.ui(self._set_update_text, "Update checks aren't enabled in this build.")
            return
        try:
            info = await asyncio.to_thread(updater.fetch_latest, REPO)
        except Exception as e:
            logging.info("update check failed: %s", e)
            if manual:
                self.ui(self._set_update_text, f"Couldn't check: {e}")
            return
        if info and updater.is_newer(info["version"], APP_VERSION):
            self.update_info = info
            self.ui(self._show_update_bar)
            if manual:
                self.ui(self._set_update_text, f"Version {info['version']} is available.")
        elif manual:
            self.ui(self._set_update_text, f"You're up to date (v{APP_VERSION}).")

    def _show_update_bar(self):
        info = self.update_info
        self.update_lbl_bar.configure(text=f"Update available · v{info['version']}")
        can_install = bool(info.get("installer_url")) and getattr(sys, "frozen", False) and CAN_SELF_UPDATE
        self.update_btn.configure(text="Install now" if can_install else "View release")
        self.update_bar.pack(fill="x", padx=24, pady=(10, 0), after=self.head)

    def _install_update(self):
        info = self.update_info
        if not info:
            return
        if not (info.get("installer_url") and getattr(sys, "frozen", False) and CAN_SELF_UPDATE):
            webbrowser.open(info.get("page") or f"https://github.com/{REPO}/releases/latest")
            return

        async def go():
            self.set_status(f"Downloading v{info['version']}...")
            path = await asyncio.to_thread(updater.download_installer, info, DATA_DIR / "update")
            self.set_status("Installing the update...")
            updater.launch_installer(path)
            self.ui(self.quit_app)
        self.run(go(), "Update failed")

    def _refresh_names(self):
        self.title_main.configure(text=self.display_name())
        self.title(self.display_name())
        self.name_entry.configure(placeholder_text=self.speaker_name or "Display name")

    @staticmethod
    def build_sources(raw):
        """Turn the speaker's raw source list into the few selectable inputs worth showing."""
        out, spotify = [], []
        for x in raw.get("sources", []):
            name, acct, status = x.get("sourceName"), x.get("sourceAccountName"), x.get("status")
            if name == "BLUETOOTH" and status == "AVAILABLE":
                out.append(("Bluetooth", "BLUETOOTH", None))
            elif name == "PRODUCT" and acct == "TV":
                out.append(("TV", "PRODUCT", "TV"))
            elif name == "SPOTIFY" and status == "AVAILABLE":
                spotify.append((x.get("displayName") or acct, acct))
            elif name == "CHROMECASTBUILTIN" and status == "AVAILABLE":
                out.append(("Chromecast", name, acct))
            elif name == "AIRPLAY" and status == "AVAILABLE":
                out.append(("AirPlay", name, acct))
        for disp, acct in spotify:
            label = "Spotify" if len(spotify) == 1 else f"Spotify · {str(disp).split('@')[0][:12]}"
            out.append((label, "SPOTIFY", acct))
        order = {"PRODUCT": 0, "BLUETOOTH": 1, "SPOTIFY": 2, "CHROMECASTBUILTIN": 3, "AIRPLAY": 4}
        return sorted(out, key=lambda c: order.get(c[1], 9))

    # ------------------------------------------------------------- state
    def _schedule_refresh(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            self.run(self._delayed_refresh())

    async def _delayed_refresh(self):
        await asyncio.sleep(0.3)
        self._refresh_pending = False
        await self._refresh()

    async def _refresh(self, sources=False):
        speaker = self.speaker
        if not speaker:
            return
        self._refresh_seq += 1
        seq = self._refresh_seq
        vol = await speaker.get_audio_volume()
        try:
            np = await speaker.get_now_playing()
        except Exception as e:
            logging.info("now playing unavailable: %s", e)
            np = {}
        try:
            power = (await speaker.get_power_state()).get("power", "ON")
        except Exception as e:
            logging.info("power state unavailable: %s", e)
            power = "ON"
        if self.speaker is not speaker:
            return  # disconnected or replaced while we were asking
        src_now = (np.get("source") or {}).get("sourceID") if np else None
        if src_now != self._last_src_seen:
            self._last_src_seen = src_now
            sources = True  # e.g. Spotify Connect just started: its account list may have changed
        if sources:
            try:
                found = self.build_sources(await speaker.get_sources())
                if found != self.all_sources or not self.all_sources:
                    self.all_sources = found
                    self._apply_source_filter()
                    self.ui(self._render_source_settings)
            except Exception as e:
                logging.info("sources unavailable: %s", e)
        self.ui(self._apply_if_newest, seq, vol, np, power)

    def _apply_if_newest(self, seq, vol, np, power):
        """Two refreshes can be in flight at once (a button press and a push notification, say). Whichever
        STARTED last wins, even if an older one finishes after it."""
        if seq < self._applied_seq:
            return
        self._applied_seq = seq
        self._apply(vol, np, power)

    def _apply(self, vol, np, power="ON"):
        self._last = (vol, np, power)
        np = np or {}
        self.cur_vol = vol["value"]
        self.muted = vol.get("muted", False)
        self.power_on = str(power).upper() not in ("OFF", "STANDBY")
        self.set_icon(self.power_btn, "", "⏻", self.accent if self.power_on else MUTED)
        if self._volume_job is None:
            shown = min(vol["value"], int(self.s["max_volume"]))
            if self.slider.get() != shown:
                self.slider.set(shown)
        self._put(self.vol_label, text=str(vol["value"]))
        self.set_icon(self.mute_btn, "" if self.muted else "", "🔇" if self.muted else "🔊",
                      RED if self.muted else TEXT)

        md = np.get("metadata") or {}
        track = np.get("track") or {}
        srcinfo = np.get("source") or {}
        self.cur_source = srcinfo.get("sourceID")
        src = srcinfo.get("sourceDisplayName") or ""
        friendly = next((lbl for lbl, s, a in self.sources if s == self.cur_source), "")
        title = md.get("trackName") or track.get("contentItem", {}).get("name") or friendly or src or "Nothing playing"
        sub = " · ".join(x for x in (md.get("artist"), md.get("album")) if x)
        self._put(self.title_lbl, text=title)
        self._put(self.sub_lbl, text=sub)
        self._put(self.src_badge, text=f"PLAYING FROM {friendly.upper()}" if friendly and title != friendly else "")
        art_url = ((track.get("contentItem") or {}).get("containerArt")
                   or ((np.get("container") or {}).get("contentItem") or {}).get("containerArt"))
        if art_url != self._art_url:
            self._art_url = art_url
            if art_url:
                cached = self._art_cache.get(art_url)
                if cached is not None:
                    self._set_art(cached)
                else:
                    self.run(self._load_art(art_url), "Album art failed")
            else:
                self._set_art(self.art_default_pil, text="♪")
        self._sync_progress(md, np.get("state") or {}, (np.get("state") or {}).get("status") == "PLAY")
        self.playing = (np.get("state") or {}).get("status") == "PLAY"
        self.set_icon(self.play_btn, "" if self.playing else "", "⏸" if self.playing else "▶")
        self._render_sources()

    # ------------------------------------------------- cover fade + progress
    def _set_art(self, pil, text=""):
        """Show `pil` on the cover label, cross-fading from whatever is shown now."""
        from PIL import Image
        if self._art_job:
            self.after_cancel(self._art_job)
            self._art_job = None
        old = self._art_pil
        if old is None or old is pil or old.size != pil.size:
            self._art_pil = pil
            self.art.configure(image=to_ctk(pil), text=text)
            return
        steps = 10

        def step(i=1):
            frame = pil if i == steps else Image.blend(old, pil, i / steps)
            try:
                self.art.configure(image=to_ctk(frame), text=text if i == steps else "")
            except tk.TclError:
                return
            self._art_pil = frame
            self._art_job = self.after(28, step, i + 1) if i < steps else None
        step()

    @staticmethod
    def fmt_time(seconds):
        s = max(0, int(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    def _sync_progress(self, md, state, playing):
        dur, pos = md.get("duration") or 0, state.get("timeIntoTrack")
        self._playing_now = playing
        self._can_seek = bool(state.get("canSeek"))
        widgets = (self.pos_lbl, self.pos_slider, self.dur_lbl)
        if dur and pos is not None:
            self._dur, self._pos_base, self._pos_t0 = float(dur), float(pos), time.monotonic()
            self._put(self.pos_slider, to=max(self._dur, 1))
            if not self._prog_shown:
                self._prog_shown = True
                for w in widgets:
                    w.grid()
            self._tick(reschedule=False)
        else:
            self._dur = 0
            if self._prog_shown:
                self._prog_shown = False
                for w in widgets:
                    w.grid_remove()

    def _tick(self, reschedule=True):
        """Advance the progress bar locally between speaker updates (the speaker only reports a snapshot)."""
        try:
            # the timer does nothing while the window is minimized / in the tray; explicit calls always update
            if self._dur and not self._seeking and (self._visible or not reschedule):
                pos = self._pos_base + ((time.monotonic() - self._pos_t0) if self._playing_now else 0)
                pos = max(0.0, min(self._dur, pos))
                if abs(self.pos_slider.get() - pos) > 0.05:
                    self.pos_slider.set(pos)
                self._put(self.pos_lbl, text=self.fmt_time(pos))
                self._put(self.dur_lbl, text=self.fmt_time(self._dur))
            if reschedule:
                moving = self._visible and self._dur and self._playing_now
                self._every("tick", 500 if moving else 1000, self._tick)
        except tk.TclError:
            pass  # window is being destroyed

    def _on_seek(self, value):
        if not self._dur or not self._can_seek:
            return  # the next tick snaps the bar back
        self._seeking = True
        self.pos_lbl.configure(text=self.fmt_time(value))
        if self._seek_job:
            self.after_cancel(self._seek_job)
        self._seek_job = self.after(350, lambda v=value: self._send_seek(v))

    def _send_seek(self, v):
        self._seek_job = None
        self._pos_base, self._pos_t0, self._seeking = float(v), time.monotonic(), False
        self.cmd("seek", int(v))

    # ---------------------------------------------------------- handlers
    def _toggle_play(self):
        self.cmd("pause" if self.playing else "play")

    def _toggle_mute(self):
        self.cmd("set_audio_volume_muted", not self.muted)

    def _on_slider(self, value):
        v = int(value)
        self.cur_vol = v
        self.vol_label.configure(text=str(v))
        if self._volume_job:
            self.after_cancel(self._volume_job)
        self._volume_job = self.after(250, lambda: self._send_volume(v))

    def _send_volume(self, v):
        self._volume_job = None
        self.cmd("set_audio_volume", v)


def main():
    if IS_WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
        except Exception:
            pass
    sock = acquire_single_instance()
    if sock is None:  # already running; it has been told to show itself
        return
    try:
        app = App(sock, start_hidden="--minimized" in sys.argv)
    except tk.TclError as e:  # e.g. Linux without a graphical session
        logging.error("could not open a window: %s", e)
        sys.stderr.write(f"{APP_NAME}: could not open a window ({e})\n")
        sys.exit(1)
    try:
        app.mainloop()
    finally:
        # Leave now. Normal interpreter shutdown waits for helper threads (tray icon, network), and a process that
        # lingers invisibly keeps the single-instance port, so the next launch would do nothing at all.
        logging.shutdown()
        os._exit(0)


if __name__ == "__main__":
    main()
