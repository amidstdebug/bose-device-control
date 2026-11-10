"""Builds the packaged app with PyInstaller. Used by build.bat / build.sh and by CI, so all of them build identically.

Windows: dist/BoseSoundbarRemote/BoseSoundbarRemote.exe
Linux:   dist/BoseSoundbarRemote/BoseSoundbarRemote   (scripts/package_linux.py turns that folder into the tarball)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from make_version_file import app_version, render  # noqa: E402

IS_WIN = sys.platform == "win32"
EXE_NAME = "BoseSoundbarRemote.exe" if IS_WIN else "BoseSoundbarRemote"


def pyinstaller_args():
    sep = ";" if IS_WIN else ":"
    args = [
        str(ROOT / "bose_app.py"),
        "--noconfirm", "--clean", "--windowed",
        "--name", "BoseSoundbarRemote",
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
        "--collect-all", "customtkinter",
        "--collect-submodules", "pybose",
        "--collect-submodules", "zeroconf",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
    ]
    if IS_WIN:
        version_file = ROOT / "build" / "version_info.txt"
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(render(app_version()), encoding="utf-8")
        args += ["--icon", str(ROOT / "assets" / "icon.ico"), "--version-file", str(version_file),
                 "--hidden-import", "pystray._win32"]
    else:
        # the tray backend and Pillow's Tk bridge are imported by name at run time, so PyInstaller can't see them
        args += ["--hidden-import", "pystray._xorg", "--hidden-import", "PIL._tkinter_finder",
                 "--hidden-import", "PIL.ImageTk"]
    return args


def main():
    import PyInstaller.__main__ as pyi

    pyi.run(pyinstaller_args())
    exe = ROOT / "dist" / "BoseSoundbarRemote" / EXE_NAME
    if not exe.exists():
        raise SystemExit(f"build finished but {exe} is missing")
    print(f"built {exe} (v{app_version()})")


if __name__ == "__main__":
    main()
