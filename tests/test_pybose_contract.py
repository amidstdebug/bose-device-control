"""pybose is an unofficial, reverse-engineered library. These tests pin down exactly what this app relies on, so
a pybose update that renames or removes something fails CI (and the weekly canary run) instead of failing for users.
"""
import inspect

import pytest

from pybose.BoseAuth import BoseAuth
from pybose.BoseDiscovery import BoseDiscovery
from pybose.BoseSpeaker import BoseSpeaker

SPEAKER_COROUTINES = {
    "connect": [], "disconnect": [], "subscribe": [], "get_system_info": [],
    "get_audio_volume": [], "set_audio_volume": ["volume"], "set_audio_volume_muted": ["muted"],
    "get_now_playing": [], "get_power_state": [], "set_power_state": ["state"],
    "play": [], "pause": [], "skip_next": [], "skip_previous": [], "seek": ["position"],
    "get_sources": [], "set_source": ["source", "sourceAccount"],
}


@pytest.mark.parametrize("name,params", SPEAKER_COROUTINES.items())
def test_speaker_methods_the_app_calls(name, params):
    method = getattr(BoseSpeaker, name)
    assert inspect.iscoroutinefunction(method), f"{name} should be async"
    sig = list(inspect.signature(method).parameters)
    for p in params:
        assert p in sig, f"{name}() lost parameter {p!r}"


def test_speaker_constructor_arguments():
    sig = inspect.signature(BoseSpeaker.__init__).parameters
    for p in ("host", "device_id", "bose_auth"):
        assert p in sig


def test_speaker_receiver_api():
    assert callable(BoseSpeaker.attach_receiver)
    assert "callback" in inspect.signature(BoseSpeaker.attach_receiver).parameters


def test_private_request_helper_used_for_source_switching():
    """The app posts /content/playbackRequest itself (to omit sourceAccount for Bluetooth)."""
    sig = inspect.signature(BoseSpeaker._request).parameters
    assert list(sig)[1:4] == ["resource", "method", "body"]
    assert inspect.iscoroutinefunction(BoseSpeaker._request)


@pytest.mark.parametrize("name", ["set_access_token", "set_azure_refresh_token", "get_azure_refresh_token",
                                  "getCachedToken", "is_token_valid", "do_token_refresh", "getControlToken"])
def test_auth_methods_the_app_calls(name):
    assert callable(getattr(BoseAuth, name))


def test_auth_token_shape_matches_what_the_app_stores():
    assert list(inspect.signature(BoseAuth.set_access_token).parameters)[1:] == [
        "access_token", "refresh_token", "bose_person_id"]
    assert list(inspect.signature(BoseAuth.getControlToken).parameters)[1:4] == ["email", "password", "forceNew"]


def test_auth_exposes_the_http_session_the_app_hooks_into():
    assert isinstance(BoseAuth()._session.hooks, dict)


def test_discovery_api():
    assert "timeout" in inspect.signature(BoseDiscovery.discover_devices).parameters


def test_response_types_have_the_fields_the_ui_reads():
    from pybose import BoseResponse as BR
    assert {"value", "muted"} <= set(BR.AudioVolume.__annotations__)
    assert {"metadata", "source", "state", "track"} <= set(BR.ContentNowPlaying.__annotations__)
    assert {"duration", "trackName", "artist", "album"} <= set(BR.Metadata.__annotations__)
    assert {"name", "productName"} <= set(BR.SystemInfo.__annotations__)
    assert "power" in BR.SystemPowerControl.__annotations__
    assert "sources" in BR.Sources.__annotations__
    assert {"sourceName", "sourceAccountName", "displayName", "status"} <= set(BR.SourceData.__annotations__)


def test_package_declares_its_runtime_dependencies_we_install():
    """pybose forgot to declare some imports in the past (requests, jwt); make sure importing it works here."""
    import importlib
    for mod in ("requests", "jwt", "websockets", "zeroconf", "cryptography"):
        importlib.import_module(mod)
