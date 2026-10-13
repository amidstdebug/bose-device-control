"""Bose Soundbar remote: customtkinter UI on top of pybose (https://github.com/cavefire/pybose)."""
import asyncio
import ctypes
import json
import logging
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


def resource(rel):
    """Path to a bundled file, both from source and from the PyInstaller exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / rel


ICON = resource("assets/icon.ico")

# Per-user data dir so it works the same from source and from the packaged .exe.
DATA_DIR = Path(os.environ.get("BOSE_REMOTE_DATA_DIR")
                or Path(os.environ.get("APPDATA", Path.home())) / "BoseSoundbarRemote")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = DATA_DIR / "bose_config.json"  # tokens, speaker address, settings (never the password)
LOGFILE = DATA_DIR / "bose_app.log"

_OLD_CONFIG = Path(__file__).parent / "bose_config.json"  # from earlier dev runs
if _OLD_CONFIG.exists() and not CONFIG.exists():
    shutil.move(str(_OLD_CONFIG), str(CONFIG))

logging.basicConfig(filename=LOGFILE, level=logging.INFO, encoding="utf-8", force=True,
                    format="%(asctime)s %(levelname)s %(message)s")

# (light, dark) tuples: customtkinter switches between them live when the appearance mode changes
BG = ("#f1f3f6", "#0f1115")
CARD = ("#ffffff", "#181b22")
BTN = ("#e6e9ef", "#0f1115")
HOVER = ("#d5d9e2", "#232834")
MUTED = ("#6b7280", "#8b93a1")
TEXT = ("#111827", "#e8eaed")
RED = ("#dc2626", "#f87171")
GREEN = ("#16a34a", "#4ade80")
SYMBOL = "Segoe UI Symbol"

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
    "window_pos": "",
}
CLOSE_ACTIONS = ["Minimize to taskbar", "Minimize to tray", "Exit"]


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def darken(hex_color, f=0.82):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


# ---------------------------------------------------------------- Windows shortcuts
def _known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def shortcut_path(kind):
    name = f"{APP_NAME}.lnk"
    if kind == "desktop":
        return _known_folder(0x10) / name                       # CSIDL_DESKTOPDIRECTORY
    if kind == "start":
        return _known_folder(0x02) / name                       # CSIDL_PROGRAMS (user Start menu > Programs)
    if kind == "startup":
        return _known_folder(0x07) / name                       # CSIDL_STARTUP
    raise ValueError(kind)


def launch_spec():
    """(target, arguments, working dir, icon) that launches this app."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", str(Path(sys.executable).parent), f"{sys.executable},0"
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw), f'"{script}"', str(script.parent), str(ICON)


def create_shortcut(kind):
    target, args, workdir, icon = launch_spec()
    if kind == "startup":
        args = (args + " --minimized").strip()
    lnk = shortcut_path(kind)
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:B_LNK);"
          "$s.TargetPath=$env:B_TGT;$s.Arguments=$env:B_ARGS;$s.WorkingDirectory=$env:B_WD;"
          "$s.IconLocation=$env:B_ICON;$s.Description='Bose Soundbar Remote';$s.Save()")
    env = {**os.environ, "B_LNK": str(lnk), "B_TGT": target, "B_ARGS": args, "B_WD": workdir, "B_ICON": icon}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       capture_output=True, text=True, creationflags=0x08000000)
    if r.returncode != 0 or not lnk.exists():
        raise RuntimeError(r.stderr.strip() or "PowerShell failed to create the shortcut")


def remove_shortcut(kind):
    shortcut_path(kind).unlink(missing_ok=True)


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

    with ThreadPoolExecutor(100) as ex:
        return [ip for ip in ex.map(check, range(1, 255)) if ip]


def acquire_single_instance(port=None):
    """Listening socket if we're the first instance; otherwise ask the running one to show itself."""
    port = port or SINGLE_INSTANCE_PORT
    srv = socket.socket()
    try:
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
    r.raise_for_status()
    data = r.raw.read(limit + 1, decode_content=True)
    if len(data) > limit:
        raise ValueError("image too large")
    return data


class App(ctk.CTk):
    def __init__(self, instance_sock, start_hidden):
        self.cfg = load_config()
        self.s = {**DEFAULTS, **self.cfg.get("settings", {})}
        ctk.set_appearance_mode(self.s["appearance"])
        super().__init__(fg_color=BG)
        if os.environ.get("BOSE_REMOTE_TEST_HIDDEN"):  # automated tests: fully transparent, no taskbar button
            self.attributes("-alpha", 0.0)
            self.attributes("-toolwindow", True)
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
        self._view = None
        self._main_view = "login"
        self._status = ("Starting...", MUTED, MUTED)
        self.tray = None

        self._ui_queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.report_callback_exception = self._tk_exception

        self._build_all()
        self._drain_ui()
        self._bind_keys()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if instance_sock:
            threading.Thread(target=self._instance_listener, args=(instance_sock,), daemon=True).start()
        if start_hidden:
            self.after(400, self._minimize_on_start)
        self.run(self._startup())

    # ------------------------------------------------------------ fonts / icons
    def _init_fonts(self):
        fams = set(tkfont.families(self))
        self.body_font = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "Arial")
        self.head_font = next((f for f in ("Segoe UI Variable Display", "Segoe UI") if f in fams), self.body_font)
        self.icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        # make every widget default to the Windows UI font instead of customtkinter's Roboto/Arial
        ctk.ThemeManager.theme["CTkFont"]["Windows"] = {"family": self.body_font, "size": 13, "weight": "normal"}

    def F(self, size=13, bold=False, head=False):
        return ctk.CTkFont(family=self.head_font if head else self.body_font, size=size,
                           weight="bold" if bold else "normal")

    def I(self, size=18):
        return ctk.CTkFont(family=self.icon_font or self.body_font, size=size)

    def icon(self, glyph, fallback):
        return glyph if self.icon_font else fallback

    # ------------------------------------------------------------ window helpers
    def _set_icon(self):
        def go():
            try:
                self.iconbitmap(str(ICON))
            except tk.TclError:
                pass
        go()
        self.after(300, go)  # customtkinter resets the icon shortly after creation

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
        try:
            import pystray
            from PIL import Image
        except ImportError:
            self.iconify()
            return
        self.withdraw()
        if self.tray is None:
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self.ui(self.show_window), default=True),
                pystray.MenuItem("Quit", lambda: self.ui(self.quit_app)),
            )
            self.tray = pystray.Icon("bose", Image.open(ICON), self.display_name(), menu)
            self.tray.run_detached()

    def _on_close(self):
        action = self.s["close_action"]
        if action == "Exit":
            self.quit_app()
        elif action == "Minimize to tray":
            self._hide_to_tray()
        else:
            self.iconify()

    def quit_app(self):
        if self.s["remember_position"]:
            try:
                self.set("window_pos", f"{self.winfo_x()},{self.winfo_y()}")
            except tk.TclError:
                pass
        if self.tray:
            self.tray.stop()
        if self.speaker:
            try:
                asyncio.run_coroutine_threadsafe(self.speaker.disconnect(), self.loop).result(timeout=3)
            except Exception:
                pass
        self.destroy()

    def _instance_listener(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        self.ui(self.show_window)
            except OSError:
                return

    # ------------------------------------------------------------ layout
    def _build_all(self):
        saved_errors = self.err_text.get("1.0", "end").strip() if hasattr(self, "err_text") else ""
        for child in self.winfo_children():
            child.destroy()
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
        return Btn(parent, text=self.icon(glyph, fallback), command=command, **base)

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
        self.play_btn = self._primary(row, self.icon("", "▶"), self._toggle_play, width=76, height=76,
                                      corner_radius=38, font=self.I(28))
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

        card = self._section("Windows integration")
        self._switch_row(card, "Desktop shortcut", shortcut_path("desktop").exists(),
                         lambda v: self._toggle_shortcut("desktop", v))
        self._switch_row(card, "Start menu entry", shortcut_path("start").exists(),
                         lambda v: self._toggle_shortcut("start", v))
        self._switch_row(card, "Run when I sign in to Windows", shortcut_path("startup").exists(),
                         lambda v: self._toggle_shortcut("startup", v))
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
        self._btn(grid, "Open data folder", lambda: os.startfile(DATA_DIR)).grid(row=0, column=0, padx=(0, 4), sticky="ew")
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
                                       font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
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
        ctk.set_appearance_mode(v)

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
        win.after(300, lambda: win.iconbitmap(str(ICON)))
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
        ctk.set_appearance_mode(self.s["appearance"])
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
        try:
            self.after(25, self._drain_ui)
        except tk.TclError:
            pass  # window is gone

    def set_status(self, text, color=MUTED, dot=None):
        self._status = (text, color, dot or self._status[2])

        def go():
            self.status.configure(text=text, text_color=color)
            if dot:
                self.dot.configure(text_color=dot)
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
                self.log_error(context, e)
        asyncio.run_coroutine_threadsafe(wrapped(), self.loop)

    def cmd(self, name, *args):
        if not self.speaker:
            self.log_error("Not connected to a soundbar yet")
            return

        async def go():
            await getattr(self.speaker, name)(*args)
            await self._refresh()
        self.run(go(), f"Command '{name}' failed")

    def _pick_source(self, source, account):
        if not self.speaker:
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
            await self.speaker._request("/content/playbackRequest", "POST", body)
            await asyncio.sleep(0.6)
            await self._refresh()
            self.set_status(f"Connected  ·  {self.cfg.get('host', '')}", GREEN, GREEN)
        self.run(go(), f"Could not switch to {label}")

    # -------------------------------------------------------- connection
    async def _startup(self):
        shutil.rmtree(DATA_DIR / "update", ignore_errors=True)  # leftovers from a previous self-update
        if self.s["check_updates"] and REPO:
            self.run(self._check_updates(), "Update check failed")
        tok = self.cfg.get("token")
        if tok:
            self.set_status("Restoring saved login...")
            try:
                await self._restore_token(tok)
            except Exception as e:
                self.log_error("Saved login no longer works, please log in again", e)
                self.cfg.pop("token", None)
                self.ui(self._set_main, "login")
                return
            await self._connect()
        else:
            self.set_status("Please log in")
            self.ui(self._set_main, "login")

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
        async def go():
            await self._disconnect()
            await self._connect()
        self.run(go(), "Reconnect failed")

    async def _disconnect(self):
        if self.speaker:
            try:
                await self.speaker.disconnect()
            except Exception as e:
                logging.warning("disconnect: %s", e)
            self.speaker = None

    async def _connect(self):
        self.ui(self._set_main, "remote")
        host, guid = self.cfg.get("host"), self.cfg.get("guid")
        try:
            if not host:
                self.set_status("Searching for soundbar on your network...")
                devices = await asyncio.to_thread(BoseDiscovery().discover_devices, 5)
                if devices:
                    host, guid = devices[0]["IP"], devices[0]["GUID"]
                else:
                    self.set_status("mDNS found nothing, scanning your network...")
                    found = await asyncio.to_thread(scan_subnet)
                    if not found:
                        self.log_error("No Bose device found by mDNS or by scanning port 8082 on this subnet.")
                        return
                    host, guid = found[0], None  # pybose learns the device id from the first message
                self.cfg.update(host=host, guid=guid)
                save_config(self.cfg)
            self.set_status(f"Connecting to {host}...")
            self.speaker = BoseSpeaker(host=host, device_id=guid, bose_auth=self.auth)
            self.speaker.attach_receiver(lambda _m: self._schedule_refresh())
            await self.speaker.connect()
        except Exception as e:
            self.speaker = None
            # a stale saved IP is a common cause; forget it so the next try rediscovers
            self.cfg.pop("host", None)
            self.cfg.pop("guid", None)
            save_config(self.cfg)
            self.log_error(f"Could not connect to the soundbar at {host}", e)
            return
        self.set_status(f"Connected  ·  {host}", GREEN, GREEN)
        try:
            await self.speaker.subscribe()  # ask the speaker to push now-playing / volume / source changes
        except Exception as e:
            logging.info("subscribe failed: %s", e)
        asyncio.create_task(self._poll(self.speaker))
        try:
            info = await self.speaker.get_system_info()
            self.speaker_name = info.get("name") or info.get("productName")
            logging.info("system info: %s", {k: info.get(k) for k in ("name", "productName", "productType")})
            self.ui(self._refresh_names)
        except Exception as e:
            logging.info("system info unavailable: %s", e)
        await self._refresh(sources=True)

    async def _poll(self, speaker):
        """Safety net: also re-read the state every few seconds in case a push notification is missed."""
        while self.speaker is speaker:
            await asyncio.sleep(POLL_SECONDS)
            if self.speaker is not speaker:
                return
            try:
                await self._refresh()
            except Exception as e:
                logging.info("poll refresh failed: %s", e)

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
        can_install = bool(info.get("installer_url")) and getattr(sys, "frozen", False)
        self.update_btn.configure(text="Install now" if can_install else "View release")
        self.update_bar.pack(fill="x", padx=24, pady=(10, 0), after=self.head)

    def _install_update(self):
        info = self.update_info
        if not info:
            return
        if not (info.get("installer_url") and getattr(sys, "frozen", False)):
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
        if not self.speaker:
            return
        vol = await self.speaker.get_audio_volume()
        try:
            np = await self.speaker.get_now_playing()
        except Exception as e:
            logging.info("now playing unavailable: %s", e)
            np = {}
        try:
            power = (await self.speaker.get_power_state()).get("power", "ON")
        except Exception as e:
            logging.info("power state unavailable: %s", e)
            power = "ON"
        src_now = (np.get("source") or {}).get("sourceID") if np else None
        if src_now != self._last_src_seen:
            self._last_src_seen = src_now
            sources = True  # e.g. Spotify Connect just started: its account list may have changed
        if sources:
            try:
                self.all_sources = self.build_sources(await self.speaker.get_sources())
                self._apply_source_filter()
                self.ui(self._render_source_settings)
            except Exception as e:
                logging.info("sources unavailable: %s", e)
        self.ui(self._apply, vol, np, power)

    def _apply(self, vol, np, power="ON"):
        self._last = (vol, np, power)
        np = np or {}
        self.cur_vol = vol["value"]
        self.muted = vol.get("muted", False)
        self.power_on = str(power).upper() not in ("OFF", "STANDBY")
        self.power_btn.configure(text_color=self.accent if self.power_on else MUTED)
        if self._volume_job is None:
            self.slider.set(min(vol["value"], int(self.s["max_volume"])))
        self.vol_label.configure(text=str(vol["value"]))
        self.mute_btn.configure(text=self.icon("" if self.muted else "", "🔇" if self.muted else "🔊"),
                                text_color=RED if self.muted else TEXT)

        md = np.get("metadata") or {}
        track = np.get("track") or {}
        srcinfo = np.get("source") or {}
        self.cur_source = srcinfo.get("sourceID")
        src = srcinfo.get("sourceDisplayName") or ""
        friendly = next((lbl for lbl, s, a in self.sources if s == self.cur_source), "")
        title = md.get("trackName") or track.get("contentItem", {}).get("name") or friendly or src or "Nothing playing"
        sub = " · ".join(x for x in (md.get("artist"), md.get("album")) if x)
        self.title_lbl.configure(text=title)
        self.sub_lbl.configure(text=sub)
        self.src_badge.configure(text=f"PLAYING FROM {friendly.upper()}" if friendly and title != friendly else "")
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
        self.play_btn.configure(text=self.icon("" if self.playing else "", "⏸" if self.playing else "▶"))
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
            self.pos_slider.configure(to=max(self._dur, 1))
            for w in widgets:
                w.grid()
            self._tick(reschedule=False)
        else:
            self._dur = 0
            for w in widgets:
                w.grid_remove()

    def _tick(self, reschedule=True):
        """Advance the progress bar locally between speaker updates (the speaker only reports a snapshot)."""
        try:
            if self._dur and not self._seeking:
                pos = self._pos_base + ((time.monotonic() - self._pos_t0) if self._playing_now else 0)
                pos = max(0.0, min(self._dur, pos))
                self.pos_slider.set(pos)
                self.pos_lbl.configure(text=self.fmt_time(pos))
                self.dur_lbl.configure(text=self.fmt_time(self._dur))
            if reschedule:
                self.after(500, self._tick)
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
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass
    sock = acquire_single_instance()
    if sock is None:  # already running; it has been told to show itself
        return
    App(sock, start_hidden="--minimized" in sys.argv).mainloop()


if __name__ == "__main__":
    main()
