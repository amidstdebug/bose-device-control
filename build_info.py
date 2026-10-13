"""Build-time constants. The release workflow overwrites this file so the packaged app knows which repository to
check for updates (REPO) and which exact version it is (VERSION). Empty values mean "development build"."""
REPO = ""     # e.g. "your-name/bose-device-control"
VERSION = ""  # e.g. "1.4.57"; when empty the app uses APP_VERSION from bose_app.py
