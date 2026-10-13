"""Check GitHub Releases for a newer version and fetch + verify its installer.

Kept free of GUI code so it can be unit tested. Network access goes through `opener`
(urllib.request.urlopen by default) so tests can inject fakes.
"""
import hashlib
import json
import re
import subprocess
import urllib.request
from urllib.parse import urlparse

API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
# the hosts a GitHub release download legitimately comes from (the asset URL redirects to the CDN)
ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")
INSTALLER_RE = re.compile(r"Setup.*\.exe$", re.IGNORECASE)
SUMS_NAME = "SHA256SUMS.txt"
MAX_DOWNLOAD = 300_000_000


def parse_version(text):
    """'v1.2.3' / '1.2.3-beta' -> (1, 2, 3); None if it isn't a version."""
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def _check_host(url):
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise ValueError(f"refusing to use {url!r}: not a https GitHub URL")


def summarize_release(release):
    """Reduce GitHub's release JSON to what the app needs."""
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in release.get("assets", [])}
    installer = next((n for n in assets if INSTALLER_RE.search(n)), None)
    return {
        "version": str(release.get("tag_name", "")).lstrip("v"),
        "page": release.get("html_url", ""),
        "notes": release.get("body") or "",
        "installer_name": installer,
        "installer_url": assets.get(installer, "") if installer else "",
        "sums_url": assets.get(SUMS_NAME, ""),
    }


def fetch_latest(repo, timeout=8, opener=urllib.request.urlopen):
    """Latest published (non-draft, non-prerelease) release as a summary dict, or None if the repo/tag is unusable."""
    if not repo or "/" not in repo:
        return None
    url = API_LATEST.format(repo=repo)
    _check_host(url)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "BoseSoundbarRemote-updater"})
    with opener(req, timeout=timeout) as resp:
        release = json.loads(resp.read().decode("utf-8"))
    info = summarize_release(release)
    return info if parse_version(info["version"]) else None


def expected_sha256(sums_text, filename):
    """Find `filename` in a `sha256sum`-style listing ("<hex>  <name>" or "<hex> *<name>")."""
    for line in sums_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*").strip() == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest, timeout, opener, limit=MAX_DOWNLOAD):
    _check_host(url)
    req = urllib.request.Request(url, headers={"User-Agent": "BoseSoundbarRemote-updater"})
    total = 0
    with opener(req, timeout=timeout) as resp, open(dest, "wb") as out:
        _check_host(resp.geturl())  # after redirects
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("download is larger than expected")
            out.write(chunk)


def download_installer(info, directory, timeout=60, opener=urllib.request.urlopen):
    """Download the installer into `directory` and verify it against the release's SHA256SUMS.txt.

    Returns the path. Raises if there is no installer, no checksum to compare to, or the hash differs.
    """
    from pathlib import Path
    if not info.get("installer_url") or not info.get("sums_url"):
        raise ValueError("this release has no installer or checksum file attached")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sums_path = directory / SUMS_NAME
    _download(info["sums_url"], sums_path, timeout, opener, limit=1_000_000)
    expected = expected_sha256(sums_path.read_text(encoding="utf-8", errors="replace"), info["installer_name"])
    if not expected:
        raise ValueError(f"{SUMS_NAME} has no entry for {info['installer_name']}")
    target = directory / info["installer_name"]
    _download(info["installer_url"], target, timeout, opener)
    actual = sha256_file(target)
    if actual != expected:
        target.unlink(missing_ok=True)
        raise ValueError("downloaded installer failed its checksum check; it was deleted")
    return target


def launch_installer(path, popen=subprocess.Popen):
    """Run the Inno Setup installer silently; it closes this app, upgrades in place and relaunches it."""
    return popen([str(path), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NOCANCEL", "/SUPPRESSMSGBOXES"],
                 close_fds=True)
