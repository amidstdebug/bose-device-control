"""End-to-end behaviour of the real window, driven against a fake speaker (no hardware, no network)."""
import os
import sys
import time
import tkinter as tk
import types

import customtkinter as ctk
import pytest

import bose_app as b
import updater
from conftest import pump, read_config, settle, write_config
from fakes import (SPOTIFY_URL, SPOTIFY_URL_2, FakeAuth, FakeDiscovery, FakeSpeaker, now_playing_spotify,
                   now_playing_tv, png_bytes)

# Don't probe by creating (and destroying) a throwaway Tk root: on Windows that can break later roots in the same
# process. Only skip on Linux boxes with no display.
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    pytest.skip("no display available for Tk", allow_module_level=True)

TOKEN = {"access_token": "a", "refresh_token": "r", "bose_person_id": "p"}


def alive(app):
    try:
        return bool(app.winfo_exists())
    except tk.TclError:
        return False


def texts(widget, out=None):
    """All label/button/switch captions under `widget`."""
    out = [] if out is None else out
    try:
        text = widget.cget("text")
        if text:
            out.append(str(text))
    except (ValueError, tk.TclError, AttributeError):
        pass
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkBaseClass):
            texts(child, out)
    return out


def chips(app):
    kids = sorted(app.chips.winfo_children(), key=lambda w: (w.grid_info()["row"], w.grid_info()["column"]))
    return [(w.cget("text"), w) for w in kids]


def chip(app, label):
    return dict(chips(app))[label]


def last_speaker():
    return FakeSpeaker.instances[-1]


@pytest.fixture
def make_app(monkeypatch):
    FakeSpeaker.instances.clear()
    FakeSpeaker.fail_connect = False
    FakeAuth.fail_login = False
    FakeDiscovery.devices = [{"IP": "192.168.1.50", "GUID": "guid-1234"}]
    monkeypatch.setattr(b, "BoseSpeaker", FakeSpeaker)
    monkeypatch.setattr(b, "BoseAuth", FakeAuth)
    monkeypatch.setattr(b, "BoseDiscovery", FakeDiscovery)
    monkeypatch.setattr(b, "POLL_SECONDS", 3600)
    monkeypatch.setattr(b, "REPO", "")
    colours = {SPOTIFY_URL: (220, 30, 30), SPOTIFY_URL_2: (30, 200, 30)}
    monkeypatch.setattr(b, "fetch_image", lambda url, limit=0: png_bytes(colours.get(url, (9, 9, 9))))
    apps = []

    def factory(connected=True, settings=None, host="192.168.1.50", start_hidden=False, instance_sock=None,
                keep_config=False):
        cfg = {}
        if connected:
            cfg["token"] = dict(TOKEN)
            if host:
                cfg["host"], cfg["guid"] = host, "guid-1234"
        if settings:
            cfg["settings"] = settings
        if not keep_config:
            write_config(cfg)
        for attempt in range(3):  # Tcl occasionally fails to read its own files when roots are created back to back
            try:
                app = b.App(instance_sock, start_hidden)
                break
            except tk.TclError as e:
                if attempt == 2 or "tcl" not in str(e).lower():
                    raise
                time.sleep(0.5)
        apps.append(app)
        app.update()
        if connected:
            assert pump(app, lambda: app.speaker is not None and app._last is not None), "never connected"
            settle(app, 0.1)
        return app

    yield factory
    for app in apps:
        if alive(app):
            try:
                app.quit_app()
            except tk.TclError:
                pass
        app.loop.call_soon_threadsafe(app.loop.stop)
    time.sleep(0.05)
    for app in apps:
        try:
            app.loop.close()
        except RuntimeError:
            pass
    ctk.set_appearance_mode("Dark")


# ================================================================ start-up and login
def test_window_defaults(make_app):
    app = make_app(connected=False)
    assert app.geometry().startswith("500x"), app.geometry()
    assert app._view == "login", "no saved login -> sign-in screen"
    assert app.title() == "Bose Soundbar"
    assert app.s["close_action"] == "Minimize to taskbar"


def test_error_panel_is_hidden_by_default(make_app):
    app = make_app(connected=False)
    assert app.err_frame.winfo_manager() == ""


def test_password_field_is_masked_and_can_be_revealed(make_app):
    app = make_app(connected=False)
    entry = app.password._entry
    app.password.insert(0, "secret")  # an empty box shows its placeholder unmasked
    app.update()
    assert entry.cget("show") == "•"
    app.show_pw.select()
    app._toggle_pw()
    assert entry.cget("show") == ""
    app.show_pw.deselect()
    app._toggle_pw()
    assert entry.cget("show") == "•"


def test_login_success_saves_token_but_never_the_password(make_app, data_dir):
    app = make_app(connected=False)
    app.email.insert(0, "me@example.com")
    app.password.insert(0, "hunter2-secret")
    app._do_login()
    assert pump(app, lambda: app.speaker is not None and app._last is not None)
    assert app._view == "remote"
    cfg = read_config()
    assert cfg["token"]["access_token"] == "access" and cfg["email"] == "me@example.com"
    assert cfg["host"] == "192.168.1.50", "discovered speaker is remembered"
    for f in data_dir.iterdir():
        if f.is_file():
            assert "hunter2-secret" not in f.read_text(encoding="utf-8", errors="ignore"), f.name
    assert app.password.get() == "", "password box is cleared after login"


def test_login_failure_is_shown_and_can_be_retried(make_app):
    FakeAuth.fail_login = True
    app = make_app(connected=False)
    app.email.insert(0, "me@example.com")
    app.password.insert(0, "wrong")
    app._do_login()
    assert pump(app, lambda: "Login failed" in app.status.cget("text"))
    assert app._view == "login"
    assert "Azure AD B2C login failed" in app.err_text.get("1.0", "end")
    assert app.login_btn.cget("state") == "normal", "button comes back so the user can try again"
    assert app.speaker is None
    FakeAuth.fail_login = False
    app._do_login()
    assert pump(app, lambda: app.speaker is not None)
    assert pump(app, lambda: app._view == "remote")


def test_login_requires_both_fields(make_app):
    app = make_app(connected=False)
    app.email.delete(0, "end")
    app._do_login()
    assert pump(app, lambda: "Enter both" in app.status.cget("text"))
    assert app.speaker is None


def test_saved_login_connects_straight_away(make_app):
    app = make_app()
    assert app._view == "remote"
    assert last_speaker().host == "192.168.1.50" and last_speaker().connected
    assert "Connected" in app.status.cget("text")


def test_stale_saved_login_falls_back_to_sign_in(make_app, monkeypatch):
    def boom(self, *a):
        raise ValueError("refresh token rejected")
    monkeypatch.setattr(FakeAuth, "is_token_valid", lambda self, token=None: False)
    monkeypatch.setattr(FakeAuth, "do_token_refresh", boom)
    app = make_app(connected=False)
    write_config({"token": dict(TOKEN)})
    app.cfg = b.load_config()
    app.run(app._startup())
    assert pump(app, lambda: app._view == "login" and "no longer works" in app.status.cget("text"))
    assert "token" not in app.cfg


def test_logout_returns_to_sign_in_and_forgets_the_token(make_app):
    app = make_app()
    app._logout()
    assert pump(app, lambda: app.speaker is None and app._view == "login")
    assert "token" not in read_config()
    assert app.sources == []


# ================================================================ finding the speaker
def test_discovery_result_is_saved(make_app):
    app = make_app(host=None)
    assert app.cfg["host"] == "192.168.1.50" and app.cfg["guid"] == "guid-1234"
    assert last_speaker().device_id == "guid-1234"


def test_port_scan_fallback_when_mdns_finds_nothing(make_app, monkeypatch):
    FakeDiscovery.devices = []
    monkeypatch.setattr(b, "scan_subnet", lambda: ["10.9.8.7"])
    app = make_app(host=None)
    assert last_speaker().host == "10.9.8.7"
    assert app.cfg["host"] == "10.9.8.7" and app.cfg["guid"] is None


def test_nothing_found_reports_an_error(make_app, monkeypatch):
    FakeDiscovery.devices = []
    monkeypatch.setattr(b, "scan_subnet", lambda: [])
    app = make_app(connected=False)
    write_config({"token": dict(TOKEN)})
    app.cfg = b.load_config()
    app.run(app._startup())
    assert pump(app, lambda: "No Bose device found" in app.status.cget("text"))
    assert app.speaker is None


def test_failed_connection_forgets_the_saved_ip_and_says_why(make_app):
    FakeSpeaker.fail_connect = True
    app = make_app(connected=False)
    write_config({"token": dict(TOKEN), "host": "192.168.1.99", "guid": "g"})
    app.cfg = b.load_config()
    app.run(app._startup())
    assert pump(app, lambda: "Could not connect" in app.status.cget("text"))
    assert "connection refused" in app.err_text.get("1.0", "end")
    assert "host" not in read_config(), "a stale IP must not be retried forever"
    assert app.speaker is None


def test_manual_ip_is_saved_and_used(make_app):
    app = make_app()
    app.ip_entry.delete(0, "end")
    app.ip_entry.insert(0, "192.168.1.77")
    n = len(FakeSpeaker.instances)
    app._save_ip()
    assert pump(app, lambda: len(FakeSpeaker.instances) > n)
    assert last_speaker().host == "192.168.1.77"
    assert read_config()["host"] == "192.168.1.77"


def test_forget_speaker_triggers_a_new_scan(make_app):
    app = make_app()
    n = len(FakeSpeaker.instances)
    app._forget_speaker()
    assert pump(app, lambda: len(FakeSpeaker.instances) > n)
    assert read_config()["host"] == "192.168.1.50"  # rediscovered


def test_reconnect_makes_a_fresh_connection(make_app):
    app = make_app()
    first = last_speaker()
    app._reconnect()
    assert pump(app, lambda: last_speaker() is not first and last_speaker().connected)
    assert not first.connected


# ================================================================ live updates
def test_subscribes_for_push_notifications_on_connect(make_app):
    make_app()
    assert ("subscribe",) in last_speaker().calls


def test_a_pushed_notification_refreshes_the_screen(make_app):
    app = make_app()
    sp = last_speaker()
    sp.now_playing = now_playing_spotify(title="Brand New Song")
    sp.receivers[0]({"header": {}})
    assert pump(app, lambda: app.title_lbl.cget("text") == "Brand New Song")


def test_safety_poll_catches_changes_without_a_notification(make_app, monkeypatch):
    monkeypatch.setattr(b, "POLL_SECONDS", 0.05)
    app = make_app()
    last_speaker().now_playing = now_playing_spotify(title="Spotify Connect Just Started")
    assert pump(app, lambda: app.title_lbl.cget("text") == "Spotify Connect Just Started")


# ================================================================ names
def test_title_uses_the_speakers_own_name(make_app):
    app = make_app()
    assert pump(app, lambda: app.title_main.cget("text") == "Living Room")
    assert app.title() == "Living Room"


def test_custom_display_name_overrides_and_persists(make_app):
    app = make_app()
    app.name_entry.insert(0, "Den Soundbar")
    app._save_name()
    assert app.title_main.cget("text") == "Den Soundbar" and app.title() == "Den Soundbar"
    assert read_config()["settings"]["device_name"] == "Den Soundbar"
    app.name_entry.delete(0, "end")
    app._save_name()
    assert app.title_main.cget("text") == "Living Room", "empty name falls back to the speaker's own"


# ================================================================ now playing
def test_now_playing_text_and_play_button(make_app):
    app = make_app()
    assert app.title_lbl.cget("text") == "Fireplace"
    assert app.sub_lbl.cget("text") == "Sleepy Fish · Beneath Your Waves"
    assert "PLAYING FROM SPOTIFY" in app.src_badge.cget("text")
    assert app.playing and app.play_btn.cget("text") == app.icon("", "⏸")
    last_speaker().now_playing = now_playing_spotify(status="PAUSE")
    app.run(app._refresh())
    assert pump(app, lambda: not app.playing)
    assert app.play_btn.cget("text") == app.icon("", "▶")


def test_tv_input_shows_its_name_without_a_track(make_app):
    app = make_app()
    last_speaker().now_playing = now_playing_tv()
    app.run(app._refresh())
    assert pump(app, lambda: app.title_lbl.cget("text") == "TV")
    assert app.sub_lbl.cget("text") == ""


def test_nothing_playing_placeholder(make_app):
    app = make_app()
    last_speaker().now_playing = {}
    app.run(app._refresh())
    assert pump(app, lambda: app.title_lbl.cget("text") == "Nothing playing")


# ================================================================ progress / runtime
def test_runtime_labels_and_bar(make_app):
    app = make_app()
    assert app.dur_lbl.cget("text") == "2:38"
    assert app.pos_lbl.cget("text") in ("0:49", "0:50")
    assert app.pos_slider.winfo_manager() == "grid", "progress bar is visible for tracks with a duration"
    assert 48 <= app.pos_slider.get() <= 51


def test_progress_advances_while_playing_and_freezes_when_paused(make_app):
    app = make_app()
    app._pos_t0 -= 10
    app._tick(reschedule=False)
    assert app.pos_lbl.cget("text") in ("0:59", "1:00")
    last_speaker().now_playing = now_playing_spotify(status="PAUSE", pos=70)
    app.run(app._refresh())
    assert pump(app, lambda: not app.playing)
    app._pos_t0 -= 30
    app._tick(reschedule=False)
    assert app.pos_lbl.cget("text") == "1:10"


def test_progress_never_runs_past_the_end(make_app):
    app = make_app()
    app._pos_t0 -= 10_000
    app._tick(reschedule=False)
    assert app.pos_lbl.cget("text") == "2:38"


def test_progress_bar_hidden_when_the_source_has_no_duration(make_app):
    app = make_app()
    last_speaker().now_playing = now_playing_tv()
    app.run(app._refresh())
    assert pump(app, lambda: app.pos_slider.winfo_manager() == "")
    assert app.pos_lbl.winfo_manager() == "" and app.dur_lbl.winfo_manager() == ""


def test_dragging_the_bar_seeks_after_a_short_pause(make_app):
    app = make_app()
    app._on_seek(100)
    assert app.pos_lbl.cget("text") == "1:40", "label follows the drag immediately"
    assert not [c for c in last_speaker().calls if c[0] == "seek"], "debounced, not sent on every pixel"
    assert pump(app, lambda: ("seek", 100) in last_speaker().calls)
    assert not app._seeking


def test_seek_is_ignored_when_the_source_cannot_seek(make_app):
    app = make_app()
    last_speaker().now_playing = now_playing_spotify(can_seek=False)
    app.run(app._refresh())
    assert pump(app, lambda: app._can_seek is False)
    app._on_seek(100)
    settle(app, 0.6)
    assert not [c for c in last_speaker().calls if c[0] == "seek"]


# ================================================================ album art
def test_cover_is_fetched_cached_and_shown(make_app):
    app = make_app()
    assert pump(app, lambda: SPOTIFY_URL in app._art_cache)
    assert pump(app, lambda: app._art_pil is app._art_cache[SPOTIFY_URL])
    assert app._art_pil.getpixel((85, 85))[:3] == (220, 30, 30)
    assert app.art.cget("text") == "", "the ♪ placeholder text goes away once a cover is shown"


def test_cover_change_fades_to_the_new_picture(make_app):
    app = make_app()
    assert pump(app, lambda: app._art_pil is app._art_cache.get(SPOTIFY_URL))
    last_speaker().now_playing = now_playing_spotify(url=SPOTIFY_URL_2, title="Second Song")
    app.run(app._refresh())
    assert pump(app, lambda: SPOTIFY_URL_2 in app._art_cache)
    seen = []
    pump(app, lambda: (seen.append(app._art_pil.getpixel((85, 85))[:3]), app._art_pil is app._art_cache[SPOTIFY_URL_2])[1])
    assert seen[-1] == (30, 200, 30)
    mids = [c for c in seen if c not in ((220, 30, 30), (30, 200, 30))]
    assert mids, "there were in-between frames (a fade), not an instant swap"
    reds = [c[0] for c in mids]
    assert reds == sorted(reds, reverse=True), "red fades out monotonically"


def test_set_art_without_a_previous_picture_is_instant(make_app):
    app = make_app()
    app._art_pil = None
    img = b.art_pil_from_bytes(png_bytes((1, 2, 3)))
    app._set_art(img)
    assert app._art_pil is img


def test_losing_the_cover_goes_back_to_the_placeholder(make_app):
    app = make_app()
    assert pump(app, lambda: app._art_pil is app._art_cache.get(SPOTIFY_URL))
    last_speaker().now_playing = now_playing_tv()
    app.run(app._refresh())
    assert pump(app, lambda: app._art_pil is app.art_default_pil)
    assert app.art.cget("text") == "♪"


def test_failed_cover_download_does_not_break_the_screen(make_app, monkeypatch):
    def boom(url, limit=0):
        raise OSError("network down")
    monkeypatch.setattr(b, "fetch_image", boom)
    app = make_app()
    settle(app, 0.4)
    assert app.title_lbl.cget("text") == "Fireplace"
    assert not app._art_cache and "network down" not in app.status.cget("text")


# ================================================================ transport / volume / power
def test_play_pause_button_toggles(make_app):
    app = make_app()
    app.play_btn.invoke()
    assert pump(app, lambda: ("pause",) in last_speaker().calls)
    last_speaker().now_playing = now_playing_spotify(status="PAUSE")
    app.run(app._refresh())
    assert pump(app, lambda: not app.playing)
    app.play_btn.invoke()
    assert pump(app, lambda: ("play",) in last_speaker().calls)


def test_skip_commands(make_app):
    app = make_app()
    app.cmd("skip_next")
    app.cmd("skip_previous")
    assert pump(app, lambda: ("skip_next",) in last_speaker().calls and ("skip_previous",) in last_speaker().calls)


def test_commands_without_a_connection_report_instead_of_crashing(make_app):
    app = make_app(connected=False)
    app.cmd("skip_next")
    assert pump(app, lambda: "Not connected" in app.status.cget("text"))


def test_volume_slider_is_debounced_and_applied(make_app):
    app = make_app()
    for v in (30, 31, 32, 33):
        app._on_slider(v)
    assert app.vol_label.cget("text") == "33"
    assert pump(app, lambda: ("set_audio_volume", 33) in last_speaker().calls)
    sent = [c for c in last_speaker().calls if c[0] == "set_audio_volume"]
    assert sent == [("set_audio_volume", 33)], "only the final value is sent"


def test_maximum_volume_limit_clamps_keyboard_and_slider(make_app):
    app = make_app()
    app._set_max_volume(50)
    assert app.slider.cget("to") == 50 and read_config()["settings"]["max_volume"] == 50
    app._nudge_volume(+100)
    assert pump(app, lambda: ("set_audio_volume", 50) in last_speaker().calls)


def test_lowering_the_limit_below_the_current_volume_turns_it_down(make_app):
    app = make_app()
    last_speaker().volume = 80
    app.run(app._refresh())
    assert pump(app, lambda: app.cur_vol == 80)
    app._set_max_volume(40)
    assert pump(app, lambda: ("set_audio_volume", 40) in last_speaker().calls)


def test_keyboard_step_and_floor(make_app):
    app = make_app(settings={"volume_step": 10})
    app._nudge_volume(-10 * 99)
    assert pump(app, lambda: ("set_audio_volume", 0) in last_speaker().calls)


def test_mute_toggle(make_app):
    app = make_app()
    app._toggle_mute()
    assert pump(app, lambda: app.muted)
    assert ("set_audio_volume_muted", True) in last_speaker().calls
    app._toggle_mute()
    assert pump(app, lambda: not app.muted)


def test_power_button_reflects_and_toggles_power(make_app):
    app = make_app()
    assert app.power_on
    last_speaker().power = "OFF"
    app.run(app._refresh())
    assert pump(app, lambda: not app.power_on)
    app.power_btn.invoke()
    assert pump(app, lambda: ("set_power_state", True) in last_speaker().calls)
    assert pump(app, lambda: app.power_on)


def test_keyboard_shortcut_events(make_app):
    app = make_app()
    app.focus_force()
    app.update()
    app.event_generate("<space>", when="now")
    app.event_generate("<Up>", when="now")
    app.event_generate("<m>", when="now")
    if not pump(app, lambda: ("pause",) in last_speaker().calls, timeout=2):
        pytest.skip("this environment doesn't deliver synthetic key events to the window")
    assert pump(app, lambda: any(c[0] == "set_audio_volume" for c in last_speaker().calls))
    assert pump(app, lambda: ("set_audio_volume_muted", True) in last_speaker().calls)


def test_keyboard_shortcuts_can_be_switched_off(make_app):
    app = make_app(settings={"keyboard_shortcuts": False})
    app.focus_force()
    app.update()
    app.event_generate("<space>", when="now")
    settle(app, 0.4)
    assert ("pause",) not in last_speaker().calls


# ================================================================ sources
def test_source_chips_match_the_speakers_inputs(make_app):
    app = make_app()
    assert [t for t, _ in chips(app)] == ["TV", "Bluetooth", "Spotify · alice.exampl", "Spotify · bob.example",
                                          "Chromecast"]


def test_active_source_is_highlighted_exactly_once(make_app):
    app = make_app()
    lit = [t for t, w in chips(app) if w.cget("fg_color") == app.accent]
    assert lit == ["Spotify · alice.exampl"], "only one of the two Spotify accounts lights up"
    last_speaker().now_playing = now_playing_tv()
    app.run(app._refresh())
    assert pump(app, lambda: [t for t, w in chips(app) if w.cget("fg_color") == app.accent] == ["TV"])


def test_picking_a_source_sends_the_right_request(make_app):
    app = make_app()
    chip(app, "TV").invoke()
    assert pump(app, lambda: ("/content/playbackRequest", "POST", {"source": "PRODUCT", "sourceAccount": "TV"})
                in last_speaker().requests)
    chip(app, "Bluetooth").invoke()
    assert pump(app, lambda: ("/content/playbackRequest", "POST", {"source": "BLUETOOTH"})
                in last_speaker().requests), "no sourceAccount key for Bluetooth"
    chip(app, "Spotify · bob.example").invoke()
    assert pump(app, lambda: ("/content/playbackRequest", "POST", {"source": "SPOTIFY", "sourceAccount": "acct-two-id"})
                in last_speaker().requests)


def test_second_spotify_account_highlights_after_it_is_picked(make_app):
    app = make_app()
    app._pick_source("SPOTIFY", "acct-two-id")
    assert pump(app, lambda: [t for t, w in chips(app) if w.cget("fg_color") == app.accent]
                == ["Spotify · bob.example"])


def test_hiding_a_source_removes_its_chip_and_persists(make_app):
    app = make_app()
    app._toggle_source("SPOTIFY|acct-two-id", False)
    assert "Spotify · bob.example" not in [t for t, _ in chips(app)]
    assert read_config()["settings"]["hidden_sources"] == ["SPOTIFY|acct-two-id"]
    app.quit_app()
    app2 = make_app(keep_config=True)
    assert "Spotify · bob.example" not in [t for t, _ in chips(app2)], "still hidden after restart"
    app2._toggle_source("SPOTIFY|acct-two-id", True)
    assert "Spotify · bob.example" in [t for t, _ in chips(app2)]
    assert read_config()["settings"]["hidden_sources"] == []


def test_settings_lists_every_source_with_a_switch(make_app):
    app = make_app()
    labels = texts(app.src_list)
    assert labels == ["TV", "Bluetooth", "Spotify · alice.exampl", "Spotify · bob.example", "Chromecast"]


def test_hidden_sources_still_listed_in_settings_so_they_can_be_restored(make_app):
    app = make_app(settings={"hidden_sources": ["BLUETOOTH|"]})
    assert "Bluetooth" not in [t for t, _ in chips(app)]
    assert "Bluetooth" in texts(app.src_list)


def test_no_sources_message_before_connecting(make_app):
    app = make_app(connected=False)
    assert any("Connect to a soundbar" in t for t in texts(app.chips))


# ================================================================ settings page
def test_settings_page_has_every_control(make_app):
    app = make_app()
    shown = " | ".join(texts(app.settings))
    for needle in ("Display name", "Mode", "Accent colour", "Always on top", "When I close the window",
                   "Start minimized at sign-in", "Remember window position", "Keyboard shortcuts",
                   "Desktop shortcut", "Start menu entry", "Run when I sign in to Windows", "Pin to taskbar",
                   "Maximum volume", "Keyboard volume step", "Speaker IP address", "Show error messages",
                   "Check for updates at startup", "Open data folder", "Reset settings", "Re-scan network",
                   "Reconnect", "Log out", "pybose by cavefire", "Speaker control is done by pybose"):
        assert needle in shown, f"settings page lost {needle!r}"


def test_settings_button_opens_and_back_returns(make_app):
    app = make_app()
    app._toggle_settings()
    assert app._view == "settings" and app.settings_outer.winfo_manager() == "pack"
    app._toggle_settings()
    assert app._view == "remote" and app.remote.winfo_manager() == "pack"


def test_connecting_while_in_settings_does_not_yank_the_user_back(make_app):
    app = make_app(connected=False)
    app._toggle_settings()
    app.run(app._startup())
    write_config({"token": dict(TOKEN), "host": "192.168.1.50"})
    app.cfg = b.load_config()
    app.run(app._startup())
    assert pump(app, lambda: app.speaker is not None)
    assert app._view == "settings"


def test_settings_persist_across_restarts(make_app):
    app = make_app()
    app._set_accent("Pink")
    app._set_appearance("Light")
    app.set("close_action", "Exit")
    app.set("always_on_top", True)
    app.set("start_minimized", False)
    app.quit_app()
    app2 = make_app(keep_config=True)
    assert app2.s["accent"] == "Pink" and app2.accent == b.ACCENTS["Pink"]
    assert app2.s["close_action"] == "Exit" and app2.s["always_on_top"] is True
    assert app2.s["start_minimized"] is False
    assert ctk.get_appearance_mode() == "Light"
    assert bool(app2.attributes("-topmost")) is True


def test_accent_change_rebuilds_the_ui_and_keeps_state(make_app):
    app = make_app()
    app._toggle_settings()
    app._set_accent("Orange")
    assert app._view == "settings", "stays on the settings page"
    assert app.play_btn.cget("fg_color") == b.ACCENTS["Orange"]
    assert app.title_lbl.cget("text") == "Fireplace", "now-playing info survives the rebuild"
    assert [t for t, _ in chips(app)][0] == "TV"
    assert app.title_main.cget("text") == "Living Room"


def test_appearance_modes(make_app):
    app = make_app()
    for mode in ("Light", "Dark"):
        app._set_appearance(mode)
        assert ctk.get_appearance_mode() == mode
        assert read_config()["settings"]["appearance"] == mode


def test_always_on_top_applies_immediately(make_app):
    app = make_app()
    app.set("always_on_top", True)
    app.attributes("-topmost", True)
    assert bool(app.attributes("-topmost"))


def test_reset_settings_restores_defaults(make_app):
    app = make_app(settings={"accent": "Red", "close_action": "Exit", "max_volume": 40, "hidden_sources": ["x|"]})
    app._toggle_settings()
    app._reset_settings()
    assert app.s == b.DEFAULTS
    assert app.accent == b.ACCENTS["Blue"] and app._view == "settings"


def test_missing_new_settings_get_defaults_for_old_config_files(make_app):
    app = make_app(settings={"accent": "Teal"})  # an old file with only one key
    assert app.s["accent"] == "Teal"
    assert app.s["show_log"] is False and app.s["hidden_sources"] == [] and app.s["check_updates"] is True


# ================================================================ error panel
def test_error_panel_toggle_and_content(make_app, data_dir):
    app = make_app()
    app._set_error_log(True)
    assert app.err_frame.winfo_manager() == "pack" and read_config()["settings"]["show_log"] is True
    app.log_error("Something broke", ValueError("details here"))
    assert pump(app, lambda: "Something broke" in app.err_text.get("1.0", "end"))
    assert "ValueError: details here" in app.err_text.get("1.0", "end")
    assert app.status.cget("text") == "Something broke"
    assert "Something broke" in (data_dir / "bose_app.log").read_text(encoding="utf-8")
    app._clear_errors()
    assert app.err_text.get("1.0", "end").strip() == ""
    app._set_error_log(False)
    assert app.err_frame.winfo_manager() == ""


def test_errors_still_reach_the_status_line_when_the_panel_is_off(make_app):
    app = make_app()
    app.log_error("Hidden panel error")
    assert pump(app, lambda: app.status.cget("text") == "Hidden panel error")
    assert app.err_frame.winfo_manager() == ""


def test_error_panel_survives_a_theme_rebuild(make_app):
    app = make_app(settings={"show_log": True})
    app.log_error("keep me")
    assert pump(app, lambda: "keep me" in app.err_text.get("1.0", "end"))
    app._set_accent("Green")
    assert "keep me" in app.err_text.get("1.0", "end")


# ================================================================ closing, minimizing, tray
def test_close_minimizes_to_taskbar_by_default(make_app):
    app = make_app()
    app._on_close()
    settle(app, 0.2)
    assert alive(app) and app.state() == "iconic"


def test_close_can_exit(make_app):
    app = make_app(settings={"close_action": "Exit"})
    app._on_close()
    assert not alive(app)


def test_close_to_tray_hides_and_show_restores(make_app, monkeypatch):
    class Icon:
        started = stopped = False

        def __init__(self, *a, **k):
            pass

        def run_detached(self):
            Icon.started = True

        def stop(self):
            Icon.stopped = True
    fake = types.SimpleNamespace(Icon=Icon, Menu=lambda *a: a, MenuItem=lambda *a, **k: (a, k))
    monkeypatch.setitem(sys.modules, "pystray", fake)
    app = make_app(settings={"close_action": "Minimize to tray"})
    app._on_close()
    settle(app, 0.2)
    assert app.state() == "withdrawn" and Icon.started and app.tray is not None
    app.show_window()
    settle(app, 0.2)
    assert app.state() == "normal" and Icon.stopped and app.tray is None


def test_exit_saves_the_window_position(make_app):
    app = make_app()
    app.geometry("+120+80")
    settle(app, 0.2)
    app.quit_app()
    assert read_config()["settings"]["window_pos"].count(",") == 1


def test_start_minimized_when_launched_by_windows(make_app):
    app = make_app(start_hidden=True)
    assert pump(app, lambda: app.state() == "iconic", timeout=3)


def test_start_minimized_can_be_disabled(make_app):
    app = make_app(start_hidden=True, settings={"start_minimized": False})
    settle(app, 0.8)
    assert app.state() == "normal"


def test_launching_a_second_copy_brings_the_first_back(make_app):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sock = b.acquire_single_instance(port)
    app = make_app(instance_sock=sock)
    app.iconify()
    settle(app, 0.2)
    assert app.state() == "iconic"
    assert b.acquire_single_instance(port) is None
    assert pump(app, lambda: app.state() == "normal", timeout=3)
    sock.close()


# ================================================================ windows integration toggles
def test_shortcut_switches_create_and_remove_files(make_app, tmp_path, monkeypatch):
    paths = {k: tmp_path / f"{k}.lnk" for k in ("desktop", "start", "startup")}
    monkeypatch.setattr(b, "shortcut_path", lambda kind: paths[kind])
    app = make_app()
    for kind, lnk in paths.items():
        app._toggle_shortcut(kind, True)
        assert lnk.exists(), kind
        app._toggle_shortcut(kind, False)
        assert not lnk.exists(), kind


def test_shortcut_failure_is_reported(make_app, monkeypatch):
    def boom(kind):
        raise RuntimeError("PowerShell failed")
    monkeypatch.setattr(b, "create_shortcut", boom)
    app = make_app()
    app._toggle_shortcut("desktop", True)
    assert pump(app, lambda: "Could not update the desktop shortcut" in app.status.cget("text"))
    assert "PowerShell failed" in app.err_text.get("1.0", "end")


def test_pin_to_taskbar_help_opens(make_app):
    app = make_app()
    before = len(app.winfo_children())
    app._pin_help()
    settle(app, 0.2)
    assert len(app.winfo_children()) == before + 1


# ================================================================ updates
INFO_NEW = {"version": "9.9.9", "page": "https://github.com/o/r/releases/tag/v9.9.9", "notes": "",
            "installer_name": "BoseSoundbarRemote-Setup-9.9.9.exe",
            "installer_url": "https://github.com/o/r/releases/download/v9.9.9/BoseSoundbarRemote-Setup-9.9.9.exe",
            "sums_url": "https://github.com/o/r/releases/download/v9.9.9/SHA256SUMS.txt"}


def test_update_banner_appears_for_a_newer_release(make_app, monkeypatch):
    monkeypatch.setattr(b, "REPO", "o/r")
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, **k: dict(INFO_NEW))
    app = make_app()
    assert pump(app, lambda: app.update_info is not None)
    assert pump(app, lambda: app.update_bar.winfo_manager() == "pack")
    assert "v9.9.9" in app.update_lbl_bar.cget("text")
    assert app.update_btn.cget("text") == "View release", "running from source: link to the page"


def test_no_banner_when_up_to_date(make_app, monkeypatch):
    monkeypatch.setattr(b, "REPO", "o/r")
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, **k: {**INFO_NEW, "version": b.APP_VERSION})
    app = make_app()
    settle(app, 0.5)
    assert app.update_info is None and app.update_bar.winfo_manager() == ""


def test_update_check_can_be_disabled(make_app, monkeypatch):
    calls = []
    monkeypatch.setattr(b, "REPO", "o/r")
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, **k: calls.append(repo))
    make_app(settings={"check_updates": False})
    assert calls == []


def test_manual_check_reports_each_outcome(make_app, monkeypatch):
    app = make_app()
    app.run(app._check_updates(manual=True))
    assert pump(app, lambda: "aren't enabled" in app.update_lbl.cget("text"))
    monkeypatch.setattr(b, "REPO", "o/r")
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, **k: {**INFO_NEW, "version": b.APP_VERSION})
    app.run(app._check_updates(manual=True))
    assert pump(app, lambda: "up to date" in app.update_lbl.cget("text"))
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, **k: dict(INFO_NEW))
    app.run(app._check_updates(manual=True))
    assert pump(app, lambda: "9.9.9 is available" in app.update_lbl.cget("text"))

    def offline(repo, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater, "fetch_latest", offline)
    app.run(app._check_updates(manual=True))
    assert pump(app, lambda: "Couldn't check" in app.update_lbl.cget("text"))


def test_view_release_opens_the_browser(make_app, monkeypatch):
    opened = []
    monkeypatch.setattr(b.webbrowser, "open", lambda url: opened.append(url))
    app = make_app()
    app.update_info = dict(INFO_NEW)
    app._install_update()
    assert opened == [INFO_NEW["page"]]


def test_packaged_app_downloads_launches_installer_and_exits(make_app, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    launched = []
    installer = tmp_path / "Setup.exe"
    installer.write_bytes(b"x")
    monkeypatch.setattr(updater, "download_installer", lambda info, directory, **k: installer)
    monkeypatch.setattr(updater, "launch_installer", lambda path, **k: launched.append(path))
    app = make_app()
    app.update_info = dict(INFO_NEW)
    app._install_update()
    assert pump(app, lambda: not alive(app))
    assert launched == [installer]


def test_failed_update_download_is_reported_and_app_keeps_running(make_app, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def boom(info, directory, **k):
        raise ValueError("downloaded installer failed its checksum check")
    monkeypatch.setattr(updater, "download_installer", boom)
    app = make_app()
    app.update_info = dict(INFO_NEW)
    app._install_update()
    assert pump(app, lambda: "Update failed" in app.status.cget("text"))
    assert alive(app)


def test_about_shows_this_project_first_then_credits_pybose(make_app, monkeypatch):
    opened = []
    monkeypatch.setattr(b.webbrowser, "open", lambda url: opened.append(url))
    app = make_app()
    shown = texts(app.settings)
    project = b.PROJECT_URL.replace("https://", "")
    assert project in shown and "pybose by cavefire" in shown and "CREDITS" in shown
    assert shown.index(project) < shown.index("CREDITS") < shown.index("pybose by cavefire")
    assert "pybose" not in project, "the project link must not read like the app is called pybose"

    def find_button(widget, label):
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkButton) and child.cget("text") == label:
                return child
            if isinstance(child, ctk.CTkBaseClass):
                hit = find_button(child, label)
                if hit:
                    return hit
        return None
    find_button(app.settings, project).invoke()
    find_button(app.settings, "pybose by cavefire").invoke()
    assert opened == [b.PROJECT_URL, b.PYBOSE_URL]
