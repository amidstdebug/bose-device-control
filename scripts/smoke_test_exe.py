"""Launches the packaged .exe and checks it really starts: a window appears, it stays up, a second launch defers to
the first, and nothing crashed. Runs against dist/ in CI and against the installed copy in the installer test.

Usage: python scripts/smoke_test_exe.py path\\to\\BoseSoundbarRemote.exe
The app is started with an isolated data folder and invisible windows, so it never touches real settings.
"""
import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path


def windows_of(pid):
    """Titles of visible top-level windows owned by `pid`."""
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
    finally:
        proc.kill()
        proc.wait()
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: smoke_test_exe.py <path to exe>")
    main(sys.argv[1])
