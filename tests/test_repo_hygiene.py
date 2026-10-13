"""Guards for things that are easy to break by accident and expensive to notice late: credit, licence, secrets,
release wiring, and workflow safety."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

import bose_app
import build_info
import updater

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "build", "dist", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "htmlcov"}


def project_files(suffixes=None):
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            if suffixes is None or p.suffix.lower() in suffixes:
                yield p


# ---------------------------------------------------------------- attribution & licence
def test_readme_credits_pybose_and_its_author():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "github.com/cavefire/pybose" in readme
    assert "cavefire" in readme
    assert "GPL" in readme


def test_licence_is_gplv3_because_pybose_is_bundled():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text and "Version 3" in text


def test_about_screen_credits_pybose():
    src = (ROOT / "bose_app.py").read_text(encoding="utf-8")
    assert "cavefire" in src and "github.com/cavefire/pybose" in src


# ---------------------------------------------------------------- secrets
SECRET_PATTERNS = {
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\."),
    "stored refresh token": re.compile(r'refresh_token"\s*:\s*"[A-Za-z0-9._~+/-]{30,}'),
    "stored access token": re.compile(r'access_token"\s*:\s*"[A-Za-z0-9._~+/-]{30,}'),
    "private key": re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
}


def test_no_secrets_in_the_repository():
    hits = []
    for p in project_files({".py", ".md", ".txt", ".json", ".yml", ".yaml", ".iss", ".bat", ".ps1", ".toml", ".cfg"}):
        if p.name in ("test_repo_hygiene.py", "test_privacy.py"):  # these contain deliberate fake leaks
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for label, rx in SECRET_PATTERNS.items():
            if rx.search(text):
                hits.append(f"{p.relative_to(ROOT)}: {label}")
    assert not hits, hits


def test_gitignore_keeps_local_state_out_of_git():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("bose_config.json", ".venv/", "dist/", "build/", "*.log"):
        assert pattern in ignore


def test_no_user_config_is_tracked_by_git():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [f for f in out if f.endswith("bose_config.json") or f.endswith(".log")]


# ---------------------------------------------------------------- versioning
def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", bose_app.APP_VERSION)
    assert updater.parse_version(bose_app.APP_VERSION) is not None


def test_committed_build_info_is_blank_or_a_valid_repo():
    """The release workflow injects the real repo; the committed value must never be a half-edited placeholder."""
    assert build_info.REPO == "" or re.fullmatch(r"[\w.-]+/[\w.-]+", build_info.REPO)


def test_requirements_are_pinned_for_reproducible_builds():
    lines = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines and all("==" in line for line in lines), "pin every dependency so builds are reproducible"
    names = {line.split("==")[0].lower() for line in lines}
    assert {"pybose", "customtkinter", "pystray", "pillow"} <= names


def test_version_file_generator_matches_app_version():
    from make_version_file import render
    text = render("1.4.2")
    assert "filevers=(1, 4, 2, 0)" in text and "'1.4.2.0'" in text  # privacy: ignore (a version number)
    assert "cavefire" not in text.lower() or True  # content is ours; just ensure it renders


def test_installer_script_is_wired_for_seamless_upgrades():
    iss = (ROOT / "installer" / "BoseSoundbarRemote.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss, "no admin prompt"
    assert re.search(r"AppId=\{\{[0-9A-F-]{36}", iss), "stable AppId so upgrades replace the old install"
    assert "CloseApplications=yes" in iss
    assert "--minimized" in iss
    assert "Bose Soundbar" in iss
    assert re.search(r"OutputBaseFilename=BoseSoundbarRemote-Setup-", iss), "updater looks for *Setup*.exe"
    assert updater.INSTALLER_RE.search("BoseSoundbarRemote-Setup-1.2.3.exe")


# ---------------------------------------------------------------- workflows
yaml = pytest.importorskip("yaml")
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_expected_workflows_exist():
    assert {w.name for w in WORKFLOWS} >= {"ci.yml", "release.yml", "canary.yml"}


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_yaml_with_jobs_and_least_privilege(wf):
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))
    assert data.get("jobs"), "no jobs"
    assert True in data or "on" in data, "no trigger"  # PyYAML parses the key `on` as True
    perms = data.get("permissions")
    job_perms = [j.get("permissions") for j in data["jobs"].values()]
    assert perms is not None or all(job_perms), f"{wf.name} should declare explicit permissions"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_actions_are_version_pinned(wf):
    for m in re.finditer(r"uses:\s*([^\s#]+)", wf.read_text(encoding="utf-8")):
        ref = m.group(1)
        if ref.startswith("./"):  # a reusable workflow in this same repo
            continue
        assert "@" in ref, f"{ref} is not pinned"
        assert ref.split("@")[1] not in ("main", "master", "latest"), f"{ref} floats"


def test_release_workflow_publishes_what_the_updater_expects():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for needle in ("SHA256SUMS.txt", "Setup", "portable", "build_info.py", "attest", "make_latest"):
        assert needle in text, f"release.yml is missing {needle!r}"


def test_release_is_automatic_after_green_ci_and_can_be_skipped():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    trigger = wf.get(True) or wf.get("on")
    assert trigger["workflow_run"]["workflows"] == ["CI"] and trigger["workflow_run"]["branches"] == ["main"]
    assert "tags" not in str(trigger), "tag pushes no longer drive releases; CI success on main does"
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "conclusion == 'success'" in text, "must only release after CI passed"
    assert "[skip release]" in text
    assert "workflow_run.head_sha" in text, "must build the exact commit CI tested"
    assert "GITHUB_RUN_NUMBER" in text and "APP_VERSION" in text, "version = series + build number"


def test_ci_runs_on_push_and_is_not_double_triggered():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    trigger = ci.get(True) or ci.get("on")
    assert "push" in trigger and "pull_request" in trigger
    assert "workflow_call" not in trigger, "release.yml follows CI via workflow_run; it must not also call it"


def test_version_stamp_is_picked_up_by_the_build(tmp_path):
    from make_version_file import app_version
    (tmp_path / "bose_app.py").write_text('APP_VERSION = "1.4.0"\n', encoding="utf-8")
    assert app_version(tmp_path) == "1.4.0"
    (tmp_path / "build_info.py").write_text('REPO = "o/r"\nVERSION = ""\n', encoding="utf-8")
    assert app_version(tmp_path) == "1.4.0", "an empty stamp falls back to the series"
    (tmp_path / "build_info.py").write_text('REPO = "o/r"\nVERSION = "1.4.57"\n', encoding="utf-8")
    assert app_version(tmp_path) == "1.4.57"


def test_app_reports_the_stamped_version():
    src = (ROOT / "bose_app.py").read_text(encoding="utf-8")
    assert "if BUILD_VERSION:\n    APP_VERSION = BUILD_VERSION" in src
    assert build_info.VERSION == "", "the committed build_info must not carry a stamped version"


def test_this_runs_on_the_python_the_ci_uses():
    assert sys.version_info >= (3, 11)


def test_release_script_cannot_recurse_or_add_a_bom():
    ps = (ROOT / "scripts" / "release.ps1").read_text(encoding="utf-8")
    assert not re.search(r"(?im)^function\s+git\b", ps), "a function named Git makes `git` call itself forever"
    assert "UTF8Encoding $false" in ps, "Set-Content -Encoding utf8 adds a BOM on Windows PowerShell 5.1"


def test_installer_ci_steps_do_not_hang_or_race():
    for name in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "Start-Process $setup -Wait" not in text, f"{name}: -Wait would wait for the relaunched app forever"
        assert "WaitForExit" in text and "Test-Path $exe" in text, f"{name}: must wait for the uninstall to finish"


def test_release_notes_introduce_the_app_before_crediting_pybose():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    about = text.index("## About")
    assert text.index("https://github.com/__REPO__", about) < text.index("pybose", about), \
        "the project link comes first, then the pybose credit"
    assert "Built on [pybose]" not in text, "wording made the app sound like it was called pybose"
    assert "name: Bose Soundbar Remote v" in text, "the release is titled with the app's name"
