"""Launches the packaged app and checks it really starts: a window appears, it stays up, a second launch defers to
the first, it quits cleanly, and nothing crashed. Runs against dist/ in CI and against the installed copy in the
installer test, on Windows and on Linux (there it needs a display, e.g. xvfb-run).

Usage: python scripts/smoke_test_exe.py path/to/BoseSoundbarRemote[.exe]
The app is started with an isolated data folder and invisible windows, so it never touches real settings.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"


def windows_of(pid):
    """Titles of visible top-level windows owned by `pid`."""
    return _windows_of_win32(pid) if IS_WIN else _windows_of_x11(pid)


def _windows_of_x11(pid):
    from Xlib import X, display
    d = display.Display()
    try:
        net_pid, net_name = d.intern_atom("_NET_WM_PID"), d.intern_atom("_NET_WM_NAME")
        found = []

        def walk(win):
            try:
                # Tk does not always set _NET_WM_PID, so a window counts unless it names a different process
                # (the test runs on its own virtual display, where nothing else opens windows)
                owner = win.get_full_property(net_pid, X.AnyPropertyType)
                if owner is None or not owner.value or int(owner.value[0]) == pid:
                    name = win.get_full_property(net_name, X.AnyPropertyType)
                    title = name.value.decode("utf-8", "replace") if name is not None else (win.get_wm_name() or "")
                    if title and win.get_attributes().map_state != X.IsUnmapped:
                        found.append(title)
                for child in win.query_tree().children:
                    walk(child)
            except Exception:  # windows come and go while we look
                pass
        walk(d.screen().root)
        return found
    finally:
        d.close()


def _windows_of_win32(pid):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            found.append(buf.value)
        return True

    user32.EnumWindows(callback, 0)
    return found


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fail(msg):
    print(f"SMOKE TEST FAILED: {msg}")
    sys.exit(1)


def main(exe):
    exe = Path(exe).resolve()
    if not exe.exists():
        fail(f"{exe} does not exist")
    if exe.stat().st_size < 1_000_000:
        fail(f"{exe.name} is suspiciously small ({exe.stat().st_size} bytes)")
    bundled_icon = list(exe.parent.rglob("icon.ico"))
    if not bundled_icon:
        fail("assets/icon.ico was not bundled (taskbar/tray icon would be missing)")

    data = Path(tempfile.mkdtemp(prefix="bose-smoke-"))
    env = {**os.environ, "BOSE_REMOTE_DATA_DIR": str(data), "BOSE_REMOTE_PORT": str(free_port()),
           "BOSE_REMOTE_TEST_HIDDEN": "1"}
    proc = subprocess.Popen([str(exe)], env=env)
    try:
        deadline = time.time() + 25
        titles = []
        while time.time() < deadline:
            if proc.poll() is not None:
                fail(f"exited early with code {proc.returncode}")
            titles = windows_of(proc.pid)
            if any("Bose Soundbar" in t for t in titles):
                break
            time.sleep(0.5)
        else:
            fail(f"no 'Bose Soundbar' window within 25 s (saw {titles})")
        print(f"ok: window {titles} appeared")

        time.sleep(4)
        if proc.poll() is not None:
            fail(f"crashed after start-up with code {proc.returncode}")
        print("ok: still running after 4 s")

        second = subprocess.Popen([str(exe)], env=env)
        try:
            second.wait(timeout=15)
        except subprocess.TimeoutExpired:
            second.kill()
            fail("a second launch did not exit (single-instance guard broken)")
        if proc.poll() is not None:
            fail("the first instance died when a second one was launched")
        print("ok: second launch deferred to the first")

        log = data / "bose_app.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if "Traceback" in text:
            fail("the log contains a traceback:\n" + text[-2000:])
        print("ok: no tracebacks in the log")

        if not IS_WIN:  # asked to stop (as at log-out), the app must go away promptly and free its port
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                fail("did not exit within 10 s of being asked to")
            print("ok: exits when asked to")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: smoke_test_exe.py <path to exe>")
    main(sys.argv[1])
