"""Stand-ins for the real Bose speaker / auth / discovery so the app can be exercised without hardware or network.

The fixture data mirrors the shapes the real soundbar returns (captured from a Soundbar 700, with the account
names replaced by made-up ones).
"""
import io
import types

SOURCES = {
    "properties": {},
    "sources": [
        {"sourceName": "GROUPING", "sourceAccountName": "grouping@bose.com", "displayName": "grouping@bose.com",
         "status": "UNAVAILABLE", "visible": True},
        {"sourceName": "AIRPLAY", "sourceAccountName": "AirPlay2DefaultUserName", "status": "UNAVAILABLE",
         "visible": True},
        {"sourceName": "GVA", "sourceAccountName": "gvauser@bose.com", "status": "AVAILABLE", "visible": True},
        {"sourceName": "CHROMECASTBUILTIN", "sourceAccountName": "chromecast_builtin@bose.com",
         "displayName": "chromecast_builtin@bose.com", "status": "AVAILABLE", "visible": True},
        {"sourceName": "SPOTIFY", "sourceAccountName": "acct-one-id", "displayName": "alice.example@example.com",
         "status": "AVAILABLE", "visible": True},
        {"sourceName": "SPOTIFY", "sourceAccountName": "acct-two-id", "displayName": "bob.example@example.com",
         "status": "AVAILABLE", "visible": True},
        {"sourceName": "SPOTIFY", "sourceAccountName": "SpotifyConnectUserName",
         "displayName": "SpotifyConnectUserName", "status": "UNAVAILABLE", "visible": True},
        {"sourceName": "ALEXA", "sourceAccountName": "alexauser@bose.com", "status": "AVAILABLE", "visible": True},
        {"sourceName": "PRODUCT", "sourceAccountName": "SLOT_0", "status": "NOT_CONFIGURED", "visible": False},
        {"sourceName": "PRODUCT", "sourceAccountName": "TV", "displayName": "TV", "status": "NOT_CONFIGURED",
         "visible": True},
        {"sourceName": "BLUETOOTH", "status": "AVAILABLE", "visible": True},
        {"sourceName": "TUNEIN", "status": "AVAILABLE", "visible": True},
    ],
}

SPOTIFY_URL = "https://i.scdn.co/image/ab67616d0000b273aaaaaaaaaaaaaaaaaaaaaaaa"
SPOTIFY_URL_2 = "https://i.scdn.co/image/ab67616d0000b273bbbbbbbbbbbbbbbbbbbbbbbb"


def now_playing_spotify(url=SPOTIFY_URL, title="Fireplace", artist="Sleepy Fish", album="Beneath Your Waves",
                        duration=158, pos=49, status="PLAY", can_seek=True):
    return {
        "metadata": {"trackName": title, "artist": artist, "album": album, "duration": duration},
        "source": {"sourceDisplayName": "Spotify", "sourceID": "SPOTIFY"},
        "state": {"status": status, "timeIntoTrack": pos, "canSeek": can_seek, "canPause": True},
        "track": {"contentItem": {"containerArt": url, "name": title, "source": "SPOTIFY"}},
    }


def now_playing_tv():
    return {
        "source": {"sourceDisplayName": "PRODUCT", "sourceID": "PRODUCT"},
        "state": {"status": "PLAY", "canPause": False, "canSeek": False},
    }


def png_bytes(color=(200, 30, 30), size=(300, 300)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeSpeaker:
    """Records every call; state is plain attributes tests can change."""
    instances = []
    fail_connect = False

    def __init__(self, host, device_id=None, bose_auth=None, **kwargs):
        self.host, self.device_id, self.bose_auth = host, device_id, bose_auth
        self.calls = []
        self.requests = []
        self.receivers = []
        self.connected = False
        self.volume, self.muted, self.power = 20, False, "ON"
        self.now_playing = now_playing_spotify()
        self.sources = SOURCES
        self.name = "Living Room"
        FakeSpeaker.instances.append(self)

    async def connect(self):
        if FakeSpeaker.fail_connect:
            raise ConnectionRefusedError("connection refused")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def attach_receiver(self, callback):
        self.receivers.append(callback)
        return len(self.receivers)

    async def subscribe(self, resources=None):
        self.calls.append(("subscribe",))
        return {}

    async def get_system_info(self):
        return {"name": self.name, "productName": "Bose Soundbar 700", "productType": "test"}

    async def get_audio_volume(self):
        return {"value": self.volume, "muted": self.muted, "min": 0, "max": 100}

    async def set_audio_volume(self, v):
        self.calls.append(("set_audio_volume", v))
        self.volume = v
        return await self.get_audio_volume()

    async def set_audio_volume_muted(self, muted):
        self.calls.append(("set_audio_volume_muted", muted))
        self.muted = muted
        return await self.get_audio_volume()

    async def get_now_playing(self):
        return self.now_playing

    async def get_power_state(self):
        return {"power": self.power}

    async def set_power_state(self, state):
        self.calls.append(("set_power_state", state))
        self.power = "ON" if state else "OFF"

    async def play(self):
        self.calls.append(("play",))

    async def pause(self):
        self.calls.append(("pause",))

    async def skip_next(self):
        self.calls.append(("skip_next",))

    async def skip_previous(self):
        self.calls.append(("skip_previous",))

    async def seek(self, position):
        self.calls.append(("seek", position))

    async def get_sources(self):
        return self.sources

    async def _request(self, resource, method, body=None, **kwargs):
        self.requests.append((resource, method, body))
        return {}


class FakeAuth:
    fail_login = False

    def __init__(self):
        self._tok = None
        self._azure = None
        self._session = types.SimpleNamespace(hooks={"response": []})

    def set_access_token(self, access, refresh, person):
        self._tok = {"access_token": access, "refresh_token": refresh, "bose_person_id": person}

    def set_azure_refresh_token(self, token):
        self._azure = token

    def get_azure_refresh_token(self):
        return self._azure or "azure-refresh-token"

    def is_token_valid(self, token=None):
        return True

    def do_token_refresh(self, *args):
        return self._tok

    def getCachedToken(self):
        return self._tok

    def getControlToken(self, email=None, password=None, forceNew=False):
        if FakeAuth.fail_login:
            raise ValueError("Azure AD B2C login failed")
        self._tok = {"access_token": "access", "refresh_token": "refresh", "bose_person_id": "person"}
        return self._tok


class FakeDiscovery:
    devices = [{"IP": "192.168.1.50", "GUID": "guid-1234"}]

    def discover_devices(self, timeout=5):
        return list(FakeDiscovery.devices)
