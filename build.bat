@echo off
rem Local build: creates dist\BoseSoundbarRemote\BoseSoundbarRemote.exe (CI uses the same scripts\build_exe.py)
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe make_icon.py
.venv\Scripts\python.exe scripts\build_exe.py
