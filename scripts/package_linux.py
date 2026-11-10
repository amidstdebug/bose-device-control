"""Turns dist/BoseSoundbarRemote (built by build_exe.py on Linux) into the tarball people download:

    dist-installer/BoseSoundbarRemote-<version>-linux-<arch>.tar.gz
        BoseSoundbarRemote-<version>/
            BoseSoundbarRemote/        the app folder (executable + its libraries)
            icon.png                   launcher icon
            install.sh, uninstall.sh   per-user install, no root needed
            README.txt, LICENSE
"""
import platform
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from make_version_file import app_version  # noqa: E402

README = """Bose Soundbar Remote {version} for Linux ({arch})

Install (for your user only, no root needed):

    ./install.sh

That copies the app to ~/.local/opt/BoseSoundbarRemote, adds it to your applications menu and creates the
command `bose-soundbar-remote` in ~/.local/bin. Run install.sh from a newer download to upgrade.

Run without installing:

    ./BoseSoundbarRemote/BoseSoundbarRemote

Remove it again:

    ./uninstall.sh            keeps your settings
    ./uninstall.sh --purge    also deletes ~/.config/BoseSoundbarRemote (login tokens, settings, log)

Needs a graphical session (X11, or Wayland with XWayland) and a 64-bit desktop Linux with glibc {glibc} or newer.
Unofficial. Not affiliated with or endorsed by Bose. Licensed under the GPL-3.0, see LICENSE.
Speaker control is done by pybose (https://github.com/cavefire/pybose) by cavefire.
"""


def arch():
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def write_icon(dest):
    from PIL import Image
    img = Image.open(ROOT / "assets" / "icon.ico")
    img.size = max(img.info.get("sizes", [img.size]))  # the largest picture stored in the .ico
    img.convert("RGBA").save(dest, "PNG")


def main(out_dir=None):
    app = ROOT / "dist" / "BoseSoundbarRemote"
    if not (app / "BoseSoundbarRemote").exists():
        raise SystemExit(f"{app} has no Linux build; run scripts/build_exe.py first")
    version = app_version()
    name = f"BoseSoundbarRemote-{version}"
    out_dir = Path(out_dir) if out_dir else ROOT / "dist-installer"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = ROOT / "build" / "linux-package" / name
    shutil.rmtree(stage.parent, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "BoseSoundbarRemote", symlinks=True)
    write_icon(stage / "icon.png")
    for script in ("install.sh", "uninstall.sh"):
        shutil.copy2(ROOT / "installer" / "linux" / script, stage / script)
        (stage / script).chmod(0o755)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    glibc = platform.libc_ver()[1] or "of the build machine"
    (stage / "README.txt").write_text(README.format(version=version, arch=arch(), glibc=glibc), encoding="utf-8")
    (stage / "VERSION").write_text(version + "\n", encoding="utf-8")
    tarball = out_dir / f"{name}-linux-{arch()}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print(f"packaged {tarball} ({tarball.stat().st_size // 1024} KiB)")
    return tarball


if __name__ == "__main__":
    main(*sys.argv[1:2])
