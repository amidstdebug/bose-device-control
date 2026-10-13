"""The privacy guard and the .gitignore safety net: these exist so nobody gets their personal data published by
accident, so they get tested properly. (All 'personal' values below are made up.)"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import check_privacy as cp  # scripts/ is on sys.path via conftest

ROOT = Path(__file__).resolve().parents[1]
HAS_GIT = (ROOT / ".git").exists() and shutil.which("git")


def kinds(text, allow=frozenset(), terms=()):
    return {kind for _, kind, _ in cp.scan_text(text, allow, terms)}


# ---------------------------------------------------------------- what must be caught
@pytest.mark.parametrize("text,kind", [
    ("contact: someone@gmail.com", "e-mail address"),
    ("owner = 'real.person@company.sg'", "e-mail address"),
    ("host = '192.168.7.23'", "IP address"),
    ("ip: 10.20.30.40", "IP address"),
    ("ip: 172.16.5.9", "IP address"),
    ("public 8.8.4.4 address", "IP address"),
    ("mac aa:bb:cc:dd:ee:ff", "MAC address"),
    ("mac AA-BB-CC-DD-EE-FF", "MAC address"),
    (r"path = C:\Users\somebody\Documents\x", "home directory"),
    ("path = c:/users/somebody/x", "home directory"),
    ("path = /Users/somebody/x", "home directory"),
    ("path = /home/somebody/x", "home directory"),
    ("id = 123e4567-e89b-12d3-a456-426614174000", "GUID"),
    ("t = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.abc'", "JWT"),
    ('{"refresh_token": "' + "A1b2C3d4" * 6 + '"}', "stored token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
    ("token=ghp_" + "a" * 36, "GitHub token"),
    ("AKIAABCDEFGHIJKLMNOP", "AWS key"),
    ("Authorization: Bearer " + "x" * 40, "Bearer token"),
])
def test_detects_personal_or_sensitive_data(text, kind):
    assert any(kind in k for k in kinds(text)), (text, kinds(text))


# ---------------------------------------------------------------- what must NOT be flagged
@pytest.mark.parametrize("text", [
    "someone@example.com", "x@example.org", "12345+name@users.noreply.github.com",
    "grouping@bose.com",
    "ip = 127.0.0.1", "ip = 0.0.0.0", "doc = 192.0.2.10", "doc = 198.51.100.7", "doc = 203.0.113.9",
    "version 1.2.3", "300.1.2.3 is not an IP",
    r"path = %USERPROFILE%\Documents", r"path = C:\Users\Public\Desktop", r"C:\Users\<name>\file",
    r"C:\Users\{user}\x", "/home/runner/work", "/home/user/x",
    "x = 'just a normal line of code'",
])
def test_ignores_harmless_text(text):
    assert not cp.scan_text(text), cp.scan_text(text)


def test_allowlist_is_exact_match_only():
    assert not cp.scan_text("ip 192.168.1.50", allow={"192.168.1.50"})
    assert cp.scan_text("ip 192.168.1.51", allow={"192.168.1.50"}), "a neighbour address is not allowed"


def test_inline_marker_skips_a_line():
    assert not cp.scan_text("x = 'someone@gmail.com'  # privacy: ignore")
    assert cp.scan_text("x = 'someone@gmail.com'")


def test_findings_are_masked_in_the_report(capsys):
    cp.report({"f.txt": [(3, "e-mail address", "very.private.person@gmail.com")]})
    out = capsys.readouterr().out
    assert "very.private.person@gmail.com" not in out and "f.txt:3" in out


# ---------------------------------------------------------------- the person running it
def test_machine_identity_is_detected_without_being_listed_anywhere(monkeypatch):
    monkeypatch.setattr(cp.getpass, "getuser", lambda: "zebra_user")
    monkeypatch.setattr(cp.socket, "gethostname", lambda: "ZEBRA-LAPTOP")
    terms = cp.personal_terms(root=Path("/nonexistent"))
    assert "zebra_user" in terms and "ZEBRA-LAPTOP" in terms
    assert kinds("backup of zebra_user's files", terms=terms)
    assert kinds("ssh ZEBRA-LAPTOP", terms=terms)
    assert not kinds("zebra_userland is a different word", terms=terms), "whole-word matching only"


def test_short_or_generic_machine_names_are_not_used(monkeypatch):
    monkeypatch.setattr(cp.getpass, "getuser", lambda: "me")
    monkeypatch.setattr(cp.socket, "gethostname", lambda: "runneradmin")
    assert "me" not in cp.machine_identity() and "runneradmin" not in cp.machine_identity()


def test_local_denylist_file_adds_your_own_terms(tmp_path):
    (tmp_path / ".privacy-deny.local").write_text("# mine\nJane Q. Public\njqp-secret-handle\n", encoding="utf-8")
    terms = cp.personal_terms(root=tmp_path)
    assert "Jane Q. Public" in terms and "jqp-secret-handle" in terms
    assert kinds("signed Jane Q. Public", terms=terms)


def test_scan_paths_reads_files_and_skips_binaries_and_the_allowlist_itself(tmp_path):
    (tmp_path / "a.txt").write_text("mail me at leak@gmail.com\n", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"leak@gmail.com\0\0binary")
    (tmp_path / ".privacy-allow").write_text("# 192.168.1.1\n", encoding="utf-8")
    res = cp.scan_paths(["a.txt", "clean.txt", "blob.bin", ".privacy-allow", "missing.txt"], root=tmp_path)
    assert list(res) == ["a.txt"]


# ---------------------------------------------------------------- the real repository
@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
def test_the_repository_itself_contains_no_personal_data():
    results = cp.scan_paths(cp.tracked_and_untracked())
    assert not results, "personal data found:\n" + "\n".join(
        f"{p}:{n} {k}: {cp.mask(t)}" for p, fs in results.items() for n, k, t in fs)


def test_allowlist_contains_only_fake_values():
    for line in cp.load_lines(ROOT / ".privacy-allow"):
        assert not cp.scan_text(line) or line in line  # entries are plain values; never a path or a name
        assert "@" not in line or line.endswith("example.com"), f"allowlisted e-mail looks real: {line}"


# ---------------------------------------------------------------- .gitignore safety net
MUST_BE_IGNORED = [
    "bose_config.json", "some/folder/bose_config.json", "bose_app.log", "logs/anything.txt", "test_progress.log",
    ".env", ".env.local", "token.json", "my_tokens.json", "secrets.txt", "credentials.json", "cert.pem",
    "id_rsa", "id_ed25519.pub", "notes/me.txt", "private/x", "local/x", "settings.local", ".privacy-deny.local",
    "Bose Soundbar.lnk", "screenshots/a.png", "Screenshot 2026-10-13.png", "Desktop.ini", "Thumbs.db",
    ".vscode/settings.json", ".idea/x", "dist/a.exe", "build/x", ".venv/x",
    "dist-installer/Setup.exe", "recording.mp4", "x.bak",
]
MUST_STAY_TRACKABLE = [
    "README.md", "bose_app.py", "updater.py", "assets/icon.ico", "tests/test_gui.py", ".privacy-allow",
    ".githooks/pre-commit", ".github/workflows/ci.yml", "installer/BoseSoundbarRemote.iss", "requirements.txt",
    "scripts/check_privacy.py", ".env.example", "LICENSE",
]


def ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT)
    return r.returncode == 0


@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_gitignore_blocks_personal_and_local_files(path):
    assert ignored(path), f"{path} would be committed!"


@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
@pytest.mark.parametrize("path", MUST_STAY_TRACKABLE)
def test_gitignore_does_not_hide_project_files(path):
    assert not ignored(path), f"{path} is wrongly ignored"


# ---------------------------------------------------------------- the pre-commit hook, end to end
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_pre_commit_hook_blocks_leaks_and_allows_clean_commits(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    shutil.copy(ROOT / "scripts" / "check_privacy.py", repo / "scripts")
    shutil.copy(ROOT / ".githooks" / "pre-commit", repo / ".githooks")
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@users.noreply.github.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@users.noreply.github.com"}

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)

    git("init", "-q")
    git("config", "core.hooksPath", ".githooks")
    (repo / "leak.txt").write_text("my email is private.person@gmail.com\n", encoding="utf-8")
    git("add", "leak.txt")
    blocked = git("commit", "-m", "leak")
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    assert "PRIVACY CHECK FAILED" in blocked.stdout + blocked.stderr
    assert "private.person@gmail.com" not in blocked.stdout + blocked.stderr, "the report must not echo the data"

    (repo / "leak.txt").write_text("my email is someone@example.com\n", encoding="utf-8")
    git("add", "leak.txt")
    ok = git("commit", "-m", "clean")
    assert ok.returncode == 0, ok.stdout + ok.stderr


@pytest.mark.skipif(not HAS_GIT, reason="needs a git checkout")
def test_hook_is_executable_in_git():
    mode = subprocess.run(["git", "ls-files", "-s", ".githooks/pre-commit"], cwd=ROOT,
                          capture_output=True, text=True).stdout.split()[:1]
    if not mode:
        pytest.skip("hook not tracked yet")
    assert mode == ["100755"], "the hook must be tracked as executable"
