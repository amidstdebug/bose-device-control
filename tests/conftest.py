import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image as _PILImage

_PILImage.init()  # see the note in bose_app.py: avoids a lazy-import deadlock between threads

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

# Must be set before bose_app is imported: it reads these at import time.
_DATA = Path(tempfile.mkdtemp(prefix="bose-remote-tests-"))
os.environ["BOSE_REMOTE_DATA_DIR"] = str(_DATA)
os.environ.setdefault("BOSE_REMOTE_PORT", "47999")
os.environ.setdefault("BOSE_REMOTE_TEST_HIDDEN", "1")  # test windows are invisible


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Every test starts with no saved config, so tests can't leak settings into each other."""
    for item in _DATA.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        elif item.name != "bose_app.log":
            item.unlink(missing_ok=True)
    return _DATA


@pytest.fixture
def data_dir():
    return _DATA


def write_config(cfg):
    (_DATA / "bose_config.json").write_text(json.dumps(cfg), encoding="utf-8")


def read_config():
    return json.loads((_DATA / "bose_config.json").read_text(encoding="utf-8"))


def pump(app, cond, timeout=15.0, interval=0.02):
    """Run the Tk event loop until `cond()` is true (the app does its work on a background asyncio thread)."""
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            return True
        time.sleep(interval)
    app.update()
    return bool(cond())


def settle(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.01)
