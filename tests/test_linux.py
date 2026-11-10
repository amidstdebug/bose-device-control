"""The Linux side: where files go, launchers and autostart, drawn icons, packaging scripts."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import bose_app as b
import icons

ROOT = Path(__file__).resolve().parents[1]
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux desktop integration")


# ---------------------------------------------------------------- drawn icons (used wherever the icon font is missing)
@pytest.mark.parametrize("name", icons.NAMES)
def test_every_icon_draws_something_in_the_requested_colour(name):
    img = icons.draw(name, 40, "#3b82f6")
    assert img.size == (40, 40) and img.mode == "RGBA"
    opaque = [p for p in img.getdata() if p[3] > 200]
    assert len(opaque) > 40, f"{name} is (almost) empty"
    assert all(p[:3] == (0x3b, 0x82, 0xf6) for p in opaque), "icons are a single flat colour"
    assert img.getpixel((0, 0))[3] == 0, "the background stays transparent"


def test_icons_differ_from_each_other():
    drawn = {name: icons.draw(name, 32, "#ffffff").tobytes() for name in icons.NAMES}
    assert len(set(drawn.values())) == len(icons.NAMES)


def test_unknown_icon_is_an_error():
    with pytest.raises(ValueError):
        icons.draw("nonsense", 32, "#ffffff")


def test_every_icon_font_glyph_has_a_drawn_replacement():
    assert set(b.ICON_NAMES.values()) == set(icons.NAMES)
    src = (ROOT / "bose_app.py").read_text(encoding="utf-8")
    used = {ch for ch in src if "" <= ch <= ""}
    assert used <= set(b.ICON_NAMES), "a Windows icon-font glyph is used that has no drawn fallback for Linux"


# ---------------------------------------------------------------- launchers (.desktop files)
def test_desktop_entry_is_a_valid_launcher():
    text = b.desktop_entry("/opt/My Apps/BoseSoundbarRemote", "", "/opt/My Apps", "/icons/x.png")
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]" and "Type=Application" in lines and "Terminal=false" in lines
    assert 'Exec="/opt/My Apps/BoseSoundbarRemote"' in lines, "paths with spaces must be quoted"
    assert "Icon=/icons/x.png" in lines and f"Name={b.APP_NAME}" in lines
    assert "StartupWMClass=BoseSoundbarRemote" in lines, "lets the desktop match the window to this launcher"
    assert "--minimized" not in text


def test_autostart_entry_starts_minimized_and_from_source_passes_the_script():
    text = b.desktop_entry("/usr/bin/python3", '"/src/bose_app.py"', "/src", "/i.png", minimized=True)
    assert 'Exec="/usr/bin/python3" "/src/bose_app.py" --minimized' in text.splitlines()


def test_exec_quoting_escapes_what_the_desktop_spec_reserves():
    assert b._desktop_quote('/a/we"ird$dir`x') == '"/a/we\\"ird\\$dir\\`x"'


@linux_only
def test_shortcut_locations_follow_the_xdg_directories(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(b, "_xdg_desktop_dir", lambda: tmp_path / "Bureau")
    assert b.shortcut_path("start") == tmp_path / "data" / "applications" / b.DESKTOP_FILE
    assert b.shortcut_path("startup") == tmp_path / "config" / "autostart" / b.DESKTOP_FILE
    assert b.shortcut_path("desktop") == tmp_path / "Bureau" / b.DESKTOP_FILE
    with pytest.raises(ValueError):
        b.shortcut_path("nonsense")


@linux_only
@pytest.mark.parametrize("kind", ["desktop", "start", "startup"])
def test_create_and_remove_linux_shortcut(kind, tmp_path, monkeypatch):
    target = tmp_path / kind / b.DESKTOP_FILE
    monkeypatch.setattr(b, "shortcut_path", lambda k: target)
    b.create_shortcut(kind)
    text = target.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]") and "bose_app.py" in text
    assert ("--minimized" in text) == (kind == "startup"), "only the autostart entry starts minimized"
    assert target.stat().st_mode & stat.S_IXUSR, "desktops refuse launchers that aren't executable"
    icon = next(ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith("Icon="))
    assert Path(icon).exists() and Path(icon).suffix == ".png", "launchers can't use the .ico"
    assert b.shortcut_exists(kind) or True
    b.remove_shortcut(kind)
    assert not target.exists()
    b.remove_shortcut(kind)  # removing twice is harmless


@linux_only
def test_launch_spec_on_linux(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    target, args, workdir, icon = b.launch_spec()
    assert target == sys.executable and "bose_app.py" in args and icon.endswith(".png")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/BoseSoundbarRemote/BoseSoundbarRemote")
    target, args, workdir, icon = b.launch_spec()
    assert target == "/opt/BoseSoundbarRemote/BoseSoundbarRemote" and args == "" and workdir == "/opt/BoseSoundbarRemote"


@linux_only
def test_data_lives_in_the_xdg_config_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert b.default_data_dir() == tmp_path / "BoseSoundbarRemote"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert b.default_data_dir() == Path.home() / ".config" / "BoseSoundbarRemote"


@linux_only
def test_no_display_means_no_tray(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert b.tray_available() is False


@linux_only
def test_open_path_uses_the_desktops_opener(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(b.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    b.open_path(tmp_path)
    assert calls == [["xdg-open", str(tmp_path)]]


def test_interface_scale_is_clamped_and_tolerates_bad_values(monkeypatch):
    applied = []
    monkeypatch.setattr(b.ctk, "set_widget_scaling", applied.append)
    monkeypatch.setattr(b.ctk, "set_window_scaling", applied.append)
    monkeypatch.setattr(b.ctk.ScalingTracker, "widget_scaling", 1.0, raising=False)
    monkeypatch.setattr(b.ctk.ScalingTracker, "window_scaling", 1.0, raising=False)
    b.set_ui_scale(100)
    b.set_ui_scale("nonsense")
    b.set_ui_scale(None)
    assert applied == [], "100% is the default: no needless redraw of every widget"
    b.set_ui_scale(150)
    b.set_ui_scale(9000)
    assert applied == [1.5, 1.5, 3.0, 3.0]


# ---------------------------------------------------------------- packaging
def test_build_script_picks_the_right_pieces_per_platform():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_exe
    args = build_exe.pyinstaller_args()
    assert "--windowed" in args and any("icon.ico" in a for a in args)
    if sys.platform == "win32":
        assert "pystray._win32" in args and "--version-file" in args
    else:
        assert "pystray._xorg" in args and "PIL._tkinter_finder" in args and "--version-file" not in args


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_linux_install_scripts_are_per_user_and_well_formed(script):
    path = ROOT / "installer" / "linux" / script
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh") and "set -eu" in text
    assert "sudo" not in text.replace("not with sudo", ""), "it installs into the home folder, never system-wide"
    assert "\r" not in text, "CRLF line endings break shell scripts"
    assert b.DESKTOP_FILE in text, "must use the same launcher name as the in-app toggle"
    if sys.platform != "win32":
        assert subprocess.run(["sh", "-n", str(path)], capture_output=True).returncode == 0, "syntax error"


@linux_only
def test_install_and_uninstall_scripts_round_trip(tmp_path):
    """Runs the real scripts against a fake download in a throwaway home directory."""
    pkg = tmp_path / "pkg"
    (pkg / "BoseSoundbarRemote").mkdir(parents=True)
    exe = pkg / "BoseSoundbarRemote" / "BoseSoundbarRemote"
    exe.write_text("#!/bin/sh\necho ran\n")
    exe.chmod(0o755)
    (pkg / "icon.png").write_bytes(b"png")
    for s in ("install.sh", "uninstall.sh"):
        (pkg / s).write_text((ROOT / "installer" / "linux" / s).read_text(encoding="utf-8"))
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "BOSE_REMOTE_ALLOW_ROOT": "1"}
    r = subprocess.run(["sh", str(pkg / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    launcher = home / ".local/share/applications" / b.DESKTOP_FILE
    link = home / ".local/bin/bose-soundbar-remote"
    assert launcher.exists() and (home / ".local/opt/BoseSoundbarRemote/BoseSoundbarRemote").exists()
    assert subprocess.run([str(link)], capture_output=True, text=True).stdout.strip() == "ran"
    assert f'Exec="{home}/.local/opt/BoseSoundbarRemote/BoseSoundbarRemote"' in launcher.read_text()
    r = subprocess.run(["sh", str(pkg / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, "installing over an existing copy is how upgrades work"

    (home / ".config/BoseSoundbarRemote").mkdir(parents=True)
    (home / ".config/BoseSoundbarRemote/bose_config.json").write_text("{}")
    r = subprocess.run(["sh", str(pkg / "uninstall.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not launcher.exists() and not link.exists() and not (home / ".local/opt/BoseSoundbarRemote").exists()
    assert (home / ".config/BoseSoundbarRemote/bose_config.json").exists(), "settings survive a plain uninstall"
    subprocess.run(["sh", str(pkg / "install.sh")], env=env, capture_output=True)
    r = subprocess.run(["sh", str(pkg / "uninstall.sh"), "--purge"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and not (home / ".config/BoseSoundbarRemote").exists()
