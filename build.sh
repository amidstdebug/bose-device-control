#!/bin/sh
# Local Linux build: creates dist/BoseSoundbarRemote/ and dist-installer/BoseSoundbarRemote-<version>-linux-<arch>.tar.gz
# (CI uses the same two scripts). Needs Python 3.11+ with tkinter: sudo apt install python3-venv python3-tk
set -eu
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/build_exe.py
.venv/bin/python scripts/package_linux.py
