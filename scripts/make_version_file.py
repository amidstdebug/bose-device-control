"""Renders the Windows version resource (Properties > Details of the .exe) for PyInstaller."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, 0),
    prodvers=({a}, {b}, {c}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Bose Soundbar Remote contributors'),
        StringStruct('FileDescription', 'Bose Soundbar Remote'),
        StringStruct('FileVersion', '{a}.{b}.{c}.0'),
        StringStruct('InternalName', 'BoseSoundbarRemote'),
        StringStruct('LegalCopyright', 'GPL-3.0. Uses pybose by cavefire.'),
        StringStruct('OriginalFilename', 'BoseSoundbarRemote.exe'),
        StringStruct('ProductName', 'Bose Soundbar Remote'),
        StringStruct('ProductVersion', '{a}.{b}.{c}.0')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def app_version(root=ROOT):
    """The version being built: the one the release workflow stamped into build_info.py, else APP_VERSION."""
    info = Path(root) / "build_info.py"
    if info.exists():
        stamped = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"', info.read_text(encoding="utf-8"), re.MULTILINE)
        if stamped:
            return stamped.group(1)
    text = (Path(root) / "bose_app.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("APP_VERSION not found in bose_app.py")
    return m.group(1)


def render(version):
    a, b, c = (int(x) for x in version.split("."))
    return TEMPLATE.format(a=a, b=b, c=c)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(app_version()), encoding="utf-8")
    print(f"wrote {out} for version {app_version()}")
