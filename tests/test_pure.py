"""Logic that needs no window: helpers, config, source list building, shortcuts, networking helpers, images."""
import io
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

import bose_app as b
from fakes import SOURCES, png_bytes

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts / COM")


# ---------------------------------------------------------------- small helpers
def test_darken_scales_each_channel():
    assert b.darken("#ffffff", 0.5) == "#7f7f7f"
    assert b.darken("#3b82f6", 1.0) == "#3b82f6"
    assert b.darken("#000000") == "#000000"


@pytest.mark.parametrize("seconds,text", [(0, "0:00"), (5, "0:05"), (59.9, "0:59"), (60, "1:00"),
                                          (158, "2:38"), (3599, "59:59"), (3600, "1:00:00"), (3725, "1:02:05"),
                                          (-4, "0:00")])
def test_fmt_time(seconds, text):
    assert b.App.fmt_time(seconds) == text


def test_resource_points_into_project_and_icon_exists():
    assert b.ICON.exists(), "assets/icon.ico must ship with the app"
    assert b.resource("assets/icon.ico") == b.ICON


# ---------------------------------------------------------------- settings / config
def test_defaults_are_sane():
    d = b.DEFAULTS
    assert d["show_log"] is False, "the error panel must be opt-in"
    assert d["close_action"] in b.CLOSE_ACTIONS
    assert d["close_action"] == "Minimize to taskbar"
    assert d["accent"] in b.ACCENTS
    assert d["appearance"] in ("Dark", "Light", "System")
    assert d["max_volume"] == 100 and d["volume_step"] in (1, 2, 5, 10)
    assert d["hidden_sources"] == []
    assert d["keyboard_shortcuts"] is True
    assert d["check_updates"] is True


def test_every_accent_is_a_valid_hex_colour():
    for name, value in b.ACCENTS.items():
        assert len(value) == 7 and value.startswith("#"), name
        int(value[1:], 16)


def test_config_roundtrip_and_corruption_tolerance(data_dir):
    assert b.load_config() == {}  # no file yet
    cfg = {"host": "192.0.2.2", "settings": {"accent": "Pink"}, "unicode": "café ♪"}
    b.save_config(cfg)
    assert b.load_config() == cfg
    (data_dir / "bose_config.json").write_text("{not json", encoding="utf-8")
    assert b.load_config() == {}  # a corrupt file must not crash the app


# ---------------------------------------------------------------- sources
def test_build_sources_keeps_only_selectable_inputs_in_order():
    # account names are shortened to 12 characters so the chips stay readable
    chips = b.App.build_sources(SOURCES)
    assert [c[0] for c in chips] == ["TV", "Bluetooth", "Spotify · alice.exampl", "Spotify · bob.example",
                                     "Chromecast"]
    assert chips[0][1:] == ("PRODUCT", "TV")
    assert chips[1][1:] == ("BLUETOOTH", None)
    assert chips[2][1:] == ("SPOTIFY", "acct-one-id")
    assert chips[4][1:] == ("CHROMECASTBUILTIN", "chromecast_builtin@bose.com")


def test_build_sources_hides_assistants_and_unavailable_inputs():
    labels = " ".join(c[0] for c in b.App.build_sources(SOURCES))
    for noise in ("Alexa", "GVA", "TuneIn", "AirPlay", "SpotifyConnectUserName", "SLOT"):
        assert noise not in labels


def test_single_spotify_account_is_just_called_spotify():
    raw = {"sources": [s for s in SOURCES["sources"] if s.get("sourceAccountName") != "acct-two-id"]}
    assert "Spotify" in [c[0] for c in b.App.build_sources(raw)]


def test_build_sources_handles_empty_and_garbage():
    assert b.App.build_sources({}) == []
    assert b.App.build_sources({"sources": []}) == []
    assert b.App.build_sources({"sources": [{"sourceName": "BLUETOOTH", "status": "UNAVAILABLE"}]}) == []


def test_source_key_is_stable_and_distinguishes_accounts():
    assert b.App._source_key(("TV", "PRODUCT", "TV")) == "PRODUCT|TV"
    assert b.App._source_key(("Bluetooth", "BLUETOOTH", None)) == "BLUETOOTH|"
    assert b.App._source_key(("a", "SPOTIFY", "1")) != b.App._source_key(("b", "SPOTIFY", "2"))


# ---------------------------------------------------------------- shortcuts
def test_launch_spec_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    target, args, workdir, icon = b.launch_spec()
    assert target.lower().endswith("pythonw.exe")
    assert "bose_app.py" in args and args.startswith('"')
    assert icon.endswith("icon.ico")


def test_launch_spec_when_packaged(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\BoseSoundbarRemote.exe")
    target, args, workdir, icon = b.launch_spec()
    assert target == r"C:\Apps\BoseSoundbarRemote.exe"
    assert args == "" and workdir == r"C:\Apps" and icon.endswith(",0")


@windows_only
def test_shortcut_paths_are_distinct_and_named_after_the_app():
    paths = {k: b.shortcut_path(k) for k in ("desktop", "start", "startup")}
    assert len(set(paths.values())) == 3
    assert all(p.name == "Bose Soundbar.lnk" for p in paths.values())
    with pytest.raises(ValueError):
        b.shortcut_path("nonsense")


@windows_only
@pytest.mark.parametrize("kind", ["desktop", "start", "startup"])
def test_create_and_remove_shortcut(kind, tmp_path, monkeypatch):
    lnk = tmp_path / "Bose Soundbar.lnk"
    monkeypatch.setattr(b, "shortcut_path", lambda k: lnk)
    b.create_shortcut(kind)
    assert lnk.exists()
    data = lnk.read_bytes()
    has_flag = "--minimized".encode("utf-16-le") in data
    assert has_flag == (kind == "startup"), "only the startup shortcut starts minimized"
    b.remove_shortcut(kind)
    assert not lnk.exists()
    b.remove_shortcut(kind)  # removing twice is harmless


# ---------------------------------------------------------------- networking helpers
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_scan_subnet_finds_open_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        port = srv.getsockname()[1]
        assert b.scan_subnet(port=port, prefix="127.0.0") == ["127.0.0.1"]
    finally:
        srv.close()


def test_scan_subnet_returns_empty_when_nothing_listens():
    assert b.scan_subnet(port=_free_port(), prefix="127.0.0") == []


def test_single_instance_second_launch_signals_first():
    port = _free_port()
    first = b.acquire_single_instance(port)
    assert first is not None
    got = []

    def accept():
        conn, _ = first.accept()
        got.append(conn.recv(16))
        conn.close()
    th = threading.Thread(target=accept, daemon=True)
    th.start()
    assert b.acquire_single_instance(port) is None  # second instance backs off...
    th.join(3)
    assert got == [b"show"]                          # ...after asking the first to show its window
    first.close()


def test_single_instance_port_is_reusable_after_exit():
    port = _free_port()
    first = b.acquire_single_instance(port)
    first.close()
    again = b.acquire_single_instance(port)
    assert again is not None
    again.close()


# ---------------------------------------------------------------- images
def test_cover_art_is_square_rounded_and_cropped():
    img = b.art_pil_from_bytes(png_bytes(size=(400, 200)), size=100)  # non-square input
    assert img.size == (100, 100) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0, "corners are transparent"
    assert img.getpixel((50, 50))[3] == 255, "centre is opaque"


def test_placeholder_art_uses_accent_colour():
    img = b.make_art_pil("#ff0000", size=100)
    r, g, bl, a = img.getpixel((50, 10))
    assert r > g and r > bl and a == 255


def test_art_rejects_non_images():
    with pytest.raises(OSError):  # PIL's UnidentifiedImageError
        b.art_pil_from_bytes(b"definitely not an image")


class _Resp:
    def __init__(self, data):
        self.raw = type("Raw", (), {"read": lambda s, n, decode_content=True: data[:n]})()

    def raise_for_status(self):
        pass


def test_fetch_image_refuses_plain_http(monkeypatch):
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(a) or _Resp(b""))
    with pytest.raises(ValueError):
        b.fetch_image("http://example.com/cover.jpg")
    assert not called, "must not even open the connection"


def test_fetch_image_returns_data_and_enforces_size_limit(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(b"x" * 100))
    assert b.fetch_image("https://i.scdn.co/image/abc") == b"x" * 100
    with pytest.raises(ValueError):
        b.fetch_image("https://i.scdn.co/image/abc", limit=10)


def test_to_ctk_keeps_size():
    img = b.make_art_pil("#3b82f6", size=64)
    assert tuple(b.to_ctk(img)._size) == (64, 64)


def test_config_file_never_contains_a_password_field(data_dir):
    b.save_config({"email": "a@example.com", "token": {"access_token": "x"}})
    assert "password" not in json.dumps(b.load_config()).lower()


def test_png_helper_is_valid():
    assert png_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert io.BytesIO(png_bytes()).readable()


def test_pillow_plugins_are_loaded_at_start_up_not_lazily_on_a_worker_thread():
    """A fresh interpreter that only imports the app must already have the PNG and JPEG plugins registered."""
    import subprocess
    code = ("import bose_app; from PIL import Image; "
            "assert 'PNG' in Image.SAVE and 'JPEG' in Image.OPEN and 'PNG' in Image.OPEN, sorted(Image.OPEN)")
    env = {**os.environ, "BOSE_REMOTE_TEST_HIDDEN": "1"}
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(b.__file__).parent, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]


def test_decoding_covers_from_many_threads_at_once_does_not_hang():
    import concurrent.futures as cf
    data = png_bytes(size=(400, 400))
    with cf.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(lambda _: b.art_pil_from_bytes(data).size, range(48), timeout=60))
    assert set(results) == {(170, 170)}
