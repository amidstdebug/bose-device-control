"""Builds dist/BoseSoundbarRemote/BoseSoundbarRemote.exe. Used by build.bat and by CI so both build identically."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from make_version_file import app_version, render  # noqa: E402


def main():
    import PyInstaller.__main__ as pyi

    version_file = ROOT / "build" / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(render(app_version()), encoding="utf-8")

    sep = ";" if sys.platform == "win32" else ":"
    pyi.run([
        str(ROOT / "bose_app.py"),
        "--noconfirm", "--clean", "--windowed",
        "--name", "BoseSoundbarRemote",
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--version-file", str(version_file),
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
        "--collect-all", "customtkinter",
        "--collect-submodules", "pybose",
        "--collect-submodules", "zeroconf",
        "--hidden-import", "pystray._win32",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
    ])
    exe = ROOT / "dist" / "BoseSoundbarRemote" / "BoseSoundbarRemote.exe"
    if not exe.exists():
        raise SystemExit(f"build finished but {exe} is missing")
    print(f"built {exe} (v{app_version()})")


if __name__ == "__main__":
    main()
