"""What keeps the app light and self-healing when it runs for days: an expired login, a speaker that goes away and
comes back, a PC that starts before its network, log and settings files that can't grow or get corrupted, and no
background work that piles up. Driven against the fake speaker, like test_gui.py."""
import gc
import json
import logging
import os
import socket
import sys
import time
import tkinter as tk

import customtkinter as ctk
import pytest

import bose_app as b
from conftest import pump, read_config, settle, write_config
from fakes import FakeAuth, FakeDiscovery, FakeSpeaker

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no display available for Tk", allow_module_level=True)

TOKEN = {"access_token": "a", "refresh_token": "r", "bose_person_id": "p"}


class Unauthorized(Exception):
    """Shaped like pybose's BoseRequestException for a rejected token."""
    http_status = 401


class NoAnswer(Exception):
    """Shaped like pybose's BoseRequestException for a speaker that stays silent."""
    http_status = 408


@pytest.fixture
def make_app(monkeypatch):
    FakeSpeaker.instances.clear()
    FakeSpeaker.fail_connect = False
    FakeDiscovery.devices = [{"IP": "192.168.1.50", "GUID": "guid-1234"}]
    monkeypatch.setattr(b, "BoseSpeaker", FakeSpeaker)
    monkeypatch.setattr(b, "BoseAuth", FakeAuth)
    monkeypatch.setattr(b, "BoseDiscovery", FakeDiscovery)
    monkeypatch.setattr(b, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(b, "MAX_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(b, "FIRST_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(b, "LOGIN_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(b, "REPO", "")
    monkeypatch.setattr(b, "fetch_image", lambda url, limit=0: (_ for _ in ()).throw(OSError("no network")))
    apps = []

    def factory(connected=True, settings=None, instance_sock=None, config=None):
        cfg = config if config is not None else ({"token": dict(TOKEN), "host": "192.168.1.50", "guid": "guid-1234"}
                                                 if connected else {})
        if settings:
            cfg["settings"] = settings
        write_config(cfg)
        app = b.App(instance_sock, False)
        apps.append(app)
        app.update()
        if connected:
            assert pump(app, lambda: app.speaker is not None and app._last is not None), "never connected"
        return app

    yield factory
    for app in apps:
        try:
            if app.winfo_exists():
                app.quit_app()
        except tk.TclError:
            pass
        app.loop.call_soon_threadsafe(app.loop.stop)
    time.sleep(0.05)
    FakeSpeaker.fail_connect = False


def status(app):
    return app.status.cget("text")


# ================================================================ the login is kept fresh
def test_an_expiring_login_is_renewed_while_the_app_keeps_running(make_app, monkeypatch):
    app = make_app()
    refreshed = []

    def refresh(self, *a):
        refreshed.append(1)
        self._tok = {"access_token": "new-access", "refresh_token": "new-refresh", "bose_person_id": "p"}
        return self._tok
    monkeypatch.setattr(FakeAuth, "do_token_refresh", refresh)
    monkeypatch.setattr(FakeAuth, "is_token_valid", lambda self, token=None: not refreshed and False or bool(refreshed))
    assert pump(app, lambda: bool(refreshed)), "the poll never renewed the expired token"
    assert pump(app, lambda: read_config()["token"]["access_token"] == "new-access"), "the new token was not saved"
    settle(app, 0.3)
    assert len(refreshed) == 1, "a valid token must not be renewed again"
    assert "Connected" in status(app)


def test_a_login_bose_refuses_to_renew_sends_the_user_back_to_sign_in(make_app, monkeypatch):
    app = make_app()

    def rejected(self, *a):
        raise b.BoseAuthRejectedError("refresh token rejected")
    monkeypatch.setattr(FakeAuth, "do_token_refresh", rejected)
    monkeypatch.setattr(FakeAuth, "is_token_valid", lambda self, token=None: False)
    assert pump(app, lambda: app._view == "login" and "expired" in status(app))
    assert app.speaker is None and "token" not in read_config()
    sp = FakeSpeaker.instances[-1]
    assert pump(app, lambda: not sp.connected), "the old connection must be closed"


def test_no_internet_while_renewing_keeps_the_login_and_retries(make_app, monkeypatch):
    app = make_app()
    attempts = []

    def offline_then_ok(self, *a):
        attempts.append(1)
        if len(attempts) < 3:
            raise b.BoseAuthUnavailableError("login service unreachable")
        return self._tok
    monkeypatch.setattr(FakeAuth, "do_token_refresh", offline_then_ok)
    monkeypatch.setattr(FakeAuth, "is_token_valid", lambda self, token=None: len(attempts) >= 3)
    assert pump(app, lambda: len(attempts) >= 3)
    assert pump(app, lambda: "Connected" in status(app))
    assert app._view == "remote" and "token" in read_config(), "a network blip must never log the user out"


def test_starting_before_the_network_is_up_waits_instead_of_logging_out(make_app, monkeypatch):
    attempts = []

    def offline_then_ok(self, *a):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("network is unreachable")
        return self._tok
    monkeypatch.setattr(FakeAuth, "do_token_refresh", offline_then_ok)
    monkeypatch.setattr(FakeAuth, "is_token_valid", lambda self, token=None: len(attempts) >= 3)
    app = make_app(connected=False, config={"token": dict(TOKEN), "host": "192.168.1.50", "guid": "g"})
    assert pump(app, lambda: app.speaker is not None and "Connected" in status(app))
    assert len(attempts) == 3 and app._view == "remote"


def test_a_command_rejected_for_an_expired_login_is_retried_after_renewing(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    sp = FakeSpeaker.instances[-1]
    refreshed = []
    monkeypatch.setattr(FakeAuth, "do_token_refresh", lambda self, *a: refreshed.append(1) or self._tok)
    sp.fail_next = [Unauthorized("JWT expired")]
    app.cmd("skip_next")
    assert pump(app, lambda: ("skip_next",) in sp.calls)
    assert refreshed == [1] and "failed" not in status(app)


# ================================================================ the connection heals itself
def test_a_dropped_connection_is_reopened_by_the_poll(make_app):
    app = make_app()
    sp = FakeSpeaker.instances[-1]
    before = sp.connects
    sp.broken = ConnectionError("WebSocket connection is closed")
    assert pump(app, lambda: sp.connects > before and sp.broken is None)
    assert pump(app, lambda: "Connected" in status(app))
    assert len(FakeSpeaker.instances) == 1, "it reconnects the same speaker object, it doesn't pile up new ones"


def test_an_unreachable_speaker_is_shown_and_recovers_when_it_returns(make_app):
    app = make_app()
    sp = FakeSpeaker.instances[-1]
    sp.heal_on_connect = False
    sp.broken = ConnectionError("no route to host")
    assert pump(app, lambda: "isn't answering" in status(app))
    assert app.err_text.get("1.0", "end").strip() == "", "a speaker that is switched off is not an error to report"
    sp.volume = 44
    sp.heal_on_connect = True
    assert pump(app, lambda: "Connected" in status(app) and app.cur_vol == 44)


def test_retries_back_off_instead_of_hammering_an_absent_speaker(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 5)
    monkeypatch.setattr(b, "MAX_RETRY_SECONDS", 60)
    assert app._poll_delay(0) == 5
    assert [app._poll_delay(n) for n in (1, 2, 3, 4, 9)] == [10, 20, 40, 60, 60]
    app._visible = False
    assert app._poll_delay(0) == 5 * b.HIDDEN_POLL_FACTOR, "minimized / in the tray: poll far less often"
    assert app._poll_delay(4) == 60


def test_a_command_on_a_dropped_connection_reconnects_and_goes_through(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    sp = FakeSpeaker.instances[-1]
    before = sp.connects
    sp.fail_next = [ConnectionError("WebSocket connection is closed")]
    app.cmd("skip_next")
    assert pump(app, lambda: ("skip_next",) in sp.calls)
    assert sp.connects == before + 1 and "failed" not in status(app)


def test_a_command_the_speaker_never_answered_is_not_sent_twice(make_app, monkeypatch):
    """It may have been carried out; skipping two tracks would be worse than reporting the failure."""
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    sp = FakeSpeaker.instances[-1]
    sp.fail_next = [NoAnswer("no response from the speaker")]
    app.cmd("skip_next")
    assert pump(app, lambda: "Command 'skip_next' failed" in status(app))
    settle(app, 0.2)
    assert ("skip_next",) not in sp.calls


def test_a_failed_first_connection_keeps_retrying_in_the_background(make_app):
    FakeSpeaker.fail_connect = True
    app = make_app(connected=False, config={"token": dict(TOKEN), "host": "192.168.1.99", "guid": "g"})
    assert pump(app, lambda: "Could not connect" in status(app))
    errors = app.err_text.get("1.0", "end")
    settle(app, 0.4)
    assert app.err_text.get("1.0", "end") == errors, "background retries must not flood the error panel"
    FakeSpeaker.fail_connect = False
    assert pump(app, lambda: app.speaker is not None and "Connected" in status(app)), "it never tried again"


def test_background_retries_do_not_port_scan_the_network_every_time(make_app, monkeypatch):
    scans = []
    FakeDiscovery.devices = []
    monkeypatch.setattr(b, "scan_subnet", lambda: scans.append(1) or [])
    app = make_app(connected=False, config={"token": dict(TOKEN)})
    assert pump(app, lambda: "No Bose device found" in status(app))
    settle(app, 0.6)  # several background retries happen in here
    assert app._retry_n >= 3 and len(scans) == 1, "the subnet scan is rate limited while retrying"


def test_pressing_reconnect_repeatedly_leaves_exactly_one_live_connection(make_app):
    app = make_app()
    for _ in range(4):
        app._reconnect()
    assert pump(app, lambda: app.speaker is not None and app._last is not None)
    settle(app, 0.5)
    live = [sp for sp in FakeSpeaker.instances if sp.connected]
    assert live == [app.speaker], f"{len(live)} connections left open"


def test_logging_out_stops_background_reconnects(make_app):
    FakeSpeaker.fail_connect = True
    app = make_app(connected=False, config={"token": dict(TOKEN), "host": "192.168.1.99", "guid": "g"})
    assert pump(app, lambda: "Could not connect" in status(app))
    app._logout()
    FakeSpeaker.fail_connect = False
    n = len(FakeSpeaker.instances)
    settle(app, 0.5)
    assert app.speaker is None and len(FakeSpeaker.instances) == n and app._view == "login"


# ================================================================ staying light
def test_unchanged_state_does_not_reconfigure_widgets(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    settle(app, 0.2)
    calls = []
    watched = (app.title_lbl, app.sub_lbl, app.src_badge, app.vol_label, app.play_btn, app.mute_btn, app.power_btn)
    for w in watched:
        orig = w.configure
        monkeypatch.setattr(w, "configure", lambda *a, _o=orig, **k: calls.append(k) or _o(*a, **k))
    app._apply(*app._last)
    assert calls == [], f"a poll that changes nothing must not redraw anything: {calls}"
    FakeSpeaker.instances[-1].volume = 61
    app.run(app._refresh())
    assert pump(app, lambda: app.vol_label.cget("text") == "61")
    assert calls == [{"text": "61"}]


def test_housekeeping_timers_are_slowed_down():
    b.tune_toolkit()
    assert ctk.AppearanceModeTracker.update_loop_interval >= 1000, "customtkinter's default is every 30 ms"
    assert ctk.ScalingTracker.update_loop_interval >= 500, "customtkinter's default is every 100 ms"


def test_timers_slow_down_while_the_window_is_hidden(make_app, monkeypatch):
    app = make_app()
    waits = []
    monkeypatch.setattr(app, "_every", lambda name, ms, fn: waits.append((name, ms)))
    app._visible = True
    app._drain_ui()
    app._visible = False
    app._drain_ui()
    app._tick()
    assert waits == [("drain", 30), ("drain", 200), ("tick", 1000)]


def test_hidden_window_skips_progress_redraws_but_catches_up_when_shown(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    settle(app, 0.2)
    app._visible = False
    app._pos_base, app._pos_t0, app._playing_now = 100.0, time.monotonic(), False
    shown = app.pos_lbl.cget("text")
    app._tick()
    assert app.pos_lbl.cget("text") == shown, "the timer does no drawing while nobody can see it"
    app._tick(reschedule=False)
    assert app.pos_lbl.cget("text") == "1:40"


def test_tk_objects_are_only_collected_on_the_tk_thread(make_app):
    """Automatic garbage collection can run on the network thread; freeing Tk objects there stalls it."""
    assert gc.isenabled()
    app = make_app()
    assert not gc.isenabled(), "while the window exists, collection is done by a timer on the Tk thread"
    assert "gc" in app._timers
    app.quit_app()
    assert gc.isenabled()


def test_quitting_frees_the_single_instance_port_and_stops_background_work(make_app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sock = b.acquire_single_instance(port)
    app = make_app(instance_sock=sock)
    sp = FakeSpeaker.instances[-1]
    app.quit_app()
    assert not sp.connected, "the speaker connection is closed on exit"
    again = None
    end = time.time() + 3  # the listener thread lets go of the socket within a moment
    while again is None and time.time() < end:
        again = b.acquire_single_instance(port)
        time.sleep(0.05)
    assert again is not None, "a leftover socket would make the next launch do nothing"
    again.close()
    app.quit_app()  # a second call (tray menu + window close) is harmless


def test_the_error_panel_cannot_grow_without_limit(make_app, monkeypatch):
    app = make_app(settings={"show_log": True})
    monkeypatch.setattr(b, "MAX_ERROR_LINES", 40)
    for i in range(200):
        app._append_error(f"problem {i}")
    text = app.err_text.get("1.0", "end")
    assert text.count("\n") <= 42 and "problem 199" in text and "problem 0\n" not in text


def test_no_tray_on_this_desktop_minimizes_instead_of_vanishing(make_app, monkeypatch):
    monkeypatch.setattr(b, "tray_available", lambda: False)
    app = make_app(settings={"close_action": "Minimize to tray"})
    app._on_close()
    assert app.tray is None
    assert pump(app, lambda: app.state() == "iconic", timeout=3), "the window must stay reachable"


# ================================================================ files
def test_settings_are_written_atomically(data_dir):
    assert b.save_config({"settings": {"accent": "Teal"}}) is True
    assert json.loads(b.CONFIG.read_text(encoding="utf-8")) == {"settings": {"accent": "Teal"}}
    assert not list(data_dir.glob("*.tmp")), "the temporary file must not be left behind"


def test_a_failed_save_keeps_the_previous_settings_intact(data_dir, monkeypatch):
    b.save_config({"token": {"access_token": "keep-me"}})

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(b.os, "replace", boom)
    assert b.save_config({"token": {}}) is False
    assert json.loads(b.CONFIG.read_text(encoding="utf-8"))["token"]["access_token"] == "keep-me"
    assert not list(data_dir.glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_the_config_file_is_private_to_the_user():
    b.save_config({"token": {"access_token": "x"}})
    assert b.CONFIG.stat().st_mode & 0o077 == 0, "it holds login tokens"


def test_the_log_file_is_size_limited():
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers, "bose_app must log through a rotating file handler"
    assert 0 < handlers[0].maxBytes <= 2_000_000 and handlers[0].backupCount >= 1


def test_repeated_log_lines_are_collapsed():
    f = b._DedupFilter()

    def rec(msg, level=logging.INFO):
        return logging.LogRecord("x", level, __file__, 1, msg, None, None)
    assert f.filter(rec("speaker not reachable"))
    assert not any(f.filter(rec("speaker not reachable")) for _ in range(500))
    last = rec("speaker is reachable again")
    assert f.filter(last)
    assert "repeated 500 more times" in last.getMessage() and "reachable again" in last.getMessage()
    assert f.filter(rec("speaker not reachable", logging.ERROR)), "a different level is a different message"


def test_noisy_libraries_only_log_problems():
    for name in ("websockets", "zeroconf", "pybose"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_image_download_always_closes_its_connection(monkeypatch):
    closed = []

    class Resp:
        raw = type("Raw", (), {"read": lambda self, n, decode_content=True: b"x" * 50})()

        def raise_for_status(self):
            raise OSError("404")

        def close(self):
            closed.append(1)
    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    with pytest.raises(OSError):
        b.fetch_image("https://example.com/a.jpg")
    assert closed == [1]
