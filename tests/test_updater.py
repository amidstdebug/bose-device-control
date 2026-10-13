import hashlib
import json

import pytest

import updater

SHA = hashlib.sha256(b"installer-bytes").hexdigest()
INSTALLER = "BoseSoundbarRemote-Setup-1.4.0.exe"


class FakeResp:
    def __init__(self, data, url="https://github.com/x"):
        self._data, self._url, self._pos = data, url, 0

    def read(self, n=-1):
        chunk = self._data[self._pos:] if n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def release(tag="v1.4.0", assets=None, body="Notes"):
    if assets is None:
        assets = [
            {"name": INSTALLER, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{INSTALLER}"},
            {"name": "BoseSoundbarRemote-1.4.0-portable.zip",
             "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/p.zip"},
            {"name": "SHA256SUMS.txt",
             "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/SHA256SUMS.txt"},
        ]
    return {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}", "body": body, "assets": assets}


# ---------------------------------------------------------------- versions
@pytest.mark.parametrize("text,expected", [("1.2.3", (1, 2, 3)), ("v1.2.3", (1, 2, 3)), (" v10.0.12 ", (10, 0, 12)),
                                           ("1.2.3-beta.1", (1, 2, 3)), ("1.2", None), ("", None), (None, None),
                                           ("latest", None)])
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


@pytest.mark.parametrize("latest,current,newer", [("1.4.0", "1.3.0", True), ("v1.3.1", "1.3.0", True),
                                                  ("2.0.0", "1.99.99", True), ("1.10.0", "1.9.0", True),
                                                  ("1.3.0", "1.3.0", False), ("1.2.9", "1.3.0", False),
                                                  ("garbage", "1.3.0", False), ("1.4.0", "garbage", False)])
def test_is_newer_compares_numerically_not_as_text(latest, current, newer):
    assert updater.is_newer(latest, current) is newer


# ---------------------------------------------------------------- release parsing
def test_summarize_release_finds_installer_and_checksums():
    info = updater.summarize_release(release())
    assert info["version"] == "1.4.0"
    assert info["installer_name"] == INSTALLER
    assert info["installer_url"].endswith(INSTALLER)
    assert info["sums_url"].endswith("SHA256SUMS.txt")
    assert info["page"].endswith("v1.4.0")


def test_summarize_release_without_installer():
    info = updater.summarize_release(release(assets=[]))
    assert info["installer_name"] is None and info["installer_url"] == "" and info["sums_url"] == ""


def test_portable_zip_is_not_mistaken_for_the_installer():
    info = updater.summarize_release(release(assets=[
        {"name": "BoseSoundbarRemote-1.4.0-portable.zip", "browser_download_url": "https://github.com/x/p.zip"}]))
    assert info["installer_name"] is None


def test_fetch_latest_parses_api_response():
    seen = {}

    def opener(req, timeout=None):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return FakeResp(json.dumps(release()).encode(), "https://api.github.com/repos/o/r/releases/latest")
    info = updater.fetch_latest("o/r", opener=opener)
    assert info["version"] == "1.4.0"
    assert seen["url"] == "https://api.github.com/repos/o/r/releases/latest" and seen["timeout"]


@pytest.mark.parametrize("repo", ["", None, "no-slash"])
def test_fetch_latest_ignores_unusable_repo(repo):
    assert updater.fetch_latest(repo, opener=lambda *a, **k: pytest.fail("must not hit the network")) is None


def test_fetch_latest_rejects_a_release_without_a_version_tag():
    opener = lambda req, timeout=None: FakeResp(json.dumps(release(tag="nightly")).encode())
    assert updater.fetch_latest("o/r", opener=opener) is None


# ---------------------------------------------------------------- checksums
def test_expected_sha256_parses_both_sha256sum_styles():
    text = f"{SHA}  {INSTALLER}\n{'a' * 64} *other.zip\n"
    assert updater.expected_sha256(text, INSTALLER) == SHA
    assert updater.expected_sha256(text, "other.zip") == "a" * 64
    assert updater.expected_sha256(text, "missing.exe") is None
    assert updater.expected_sha256("not-a-hash  x.exe", "x.exe") is None


# ---------------------------------------------------------------- download + verify
def make_opener(files, redirect_to=None):
    def opener(req, timeout=None):
        url = req.full_url
        name = url.rsplit("/", 1)[-1]
        return FakeResp(files[name], redirect_to or url)
    return opener


def info_for(tag="v1.4.0"):
    return updater.summarize_release(release(tag=tag))


def test_download_installer_verifies_and_returns_path(tmp_path):
    files = {INSTALLER: b"installer-bytes", "SHA256SUMS.txt": f"{SHA}  {INSTALLER}\n".encode()}
    path = updater.download_installer(info_for(), tmp_path, opener=make_opener(files))
    assert path.name == INSTALLER and path.read_bytes() == b"installer-bytes"


def test_download_installer_deletes_a_tampered_file(tmp_path):
    files = {INSTALLER: b"tampered!", "SHA256SUMS.txt": f"{SHA}  {INSTALLER}\n".encode()}
    with pytest.raises(ValueError, match="checksum"):
        updater.download_installer(info_for(), tmp_path, opener=make_opener(files))
    assert not (tmp_path / INSTALLER).exists()


def test_download_installer_requires_a_checksum_entry(tmp_path):
    files = {INSTALLER: b"installer-bytes", "SHA256SUMS.txt": b"\n"}
    with pytest.raises(ValueError):
        updater.download_installer(info_for(), tmp_path, opener=make_opener(files))


def test_download_installer_requires_installer_and_sums_assets(tmp_path):
    with pytest.raises(ValueError):
        updater.download_installer(updater.summarize_release(release(assets=[])), tmp_path, opener=lambda *a, **k: None)


def test_download_refuses_redirect_to_foreign_host(tmp_path):
    files = {INSTALLER: b"installer-bytes", "SHA256SUMS.txt": f"{SHA}  {INSTALLER}\n".encode()}
    with pytest.raises(ValueError, match="GitHub"):
        updater.download_installer(info_for(), tmp_path, opener=make_opener(files, redirect_to="https://evil.example/x"))


@pytest.mark.parametrize("url", ["http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x",
                                 "ftp://github.com/x", "https://notgithub.com/x"])
def test_host_check_rejects_lookalikes(url):
    with pytest.raises(ValueError):
        updater._check_host(url)


@pytest.mark.parametrize("url", ["https://github.com/o/r/releases/download/v1/x.exe",
                                 "https://api.github.com/repos/o/r/releases/latest",
                                 "https://objects.githubusercontent.com/abc"])
def test_host_check_accepts_github_hosts(url):
    updater._check_host(url)


def test_download_enforces_size_limit(tmp_path):
    with pytest.raises(ValueError, match="larger"):
        updater._download("https://github.com/x", tmp_path / "f",
                          5, make_opener({"x": b"y" * 100}), limit=10)


def test_launch_installer_runs_silent_upgrade_flags(tmp_path):
    seen = {}
    updater.launch_installer(tmp_path / "Setup.exe", popen=lambda args, **k: seen.setdefault("args", args))
    flags = seen["args"][1:]
    assert str(tmp_path / "Setup.exe") == seen["args"][0]
    for flag in ("/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"):
        assert flag in flags
