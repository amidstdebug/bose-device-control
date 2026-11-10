#!/bin/sh
# Run from source on Linux (after: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
cd "$(dirname "$0")"
exec .venv/bin/python bose_app.py "$@"
