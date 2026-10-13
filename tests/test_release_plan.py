"""Runs the release workflow's 'plan' script (the logic that decides whether to publish, and which version) against
real throwaway git repositories, so the automatic-release rules are tested, not just written down."""
import codecs
import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(not (PS and shutil.which("git")), reason="needs PowerShell and git")


def plan_script():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["plan"]["steps"] if s.get("id") == "plan")
    return step["run"]


class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@users.noreply.github.com")
        self.git("config", "user.name", "t")
        self.write("bose_app.py", 'APP_VERSION = "1.4.0"\n')
        self.write("README.md", "hello\n")
        self.commit("first")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=True).stdout

    def write(self, name, text):
        f = self.path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def plan(self, run_number=57, force=False):
        out = self.path / "gh_output.txt"
        out.write_text("", encoding="utf-8")
        script = self.path / "plan.ps1"
        script.write_text(plan_script().replace("${{ inputs.force }}", "true" if force else "false"),
                          encoding="utf-8")
        env = {**os.environ, "GITHUB_RUN_NUMBER": str(run_number), "GITHUB_OUTPUT": str(out)}
        r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=self.path,
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        raw = out.read_bytes()  # Windows PowerShell 5.1 writes UTF-16; the runners' PowerShell 7 writes UTF-8
        text = raw.decode("utf-16") if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else raw.decode("utf-8")
        return dict(line.strip().split("=", 1) for line in text.splitlines() if "=" in line)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_first_ever_push_is_released_with_series_and_build_number(repo):
    out = repo.plan(run_number=57)
    assert out["publish"] == "true" and out["version"] == "1.4.57" and out["prev"] == ""
    assert len(out["sha"]) == 40


def test_app_change_after_a_release_publishes_again(repo):
    repo.git("tag", "v1.4.5")
    repo.write("bose_app.py", 'APP_VERSION = "1.4.0"\n# a change\n')
    repo.commit("fix something")
    out = repo.plan(run_number=60)
    assert out["publish"] == "true" and out["version"] == "1.4.60" and out["prev"] == "v1.4.5"


@pytest.mark.parametrize("path", ["README.md", "tests/test_x.py", "LICENSE", ".github/PULL_REQUEST_TEMPLATE.md",
                                  ".github/workflows/ci.yml", "pyproject.toml", "scripts/check_privacy.py"])
def test_docs_tests_and_plumbing_do_not_trigger_a_release(repo, path):
    repo.git("tag", "v1.4.5")
    repo.write(path, "changed\n")
    repo.commit("tweak")
    assert repo.plan()["publish"] == "false"


@pytest.mark.parametrize("path", ["updater.py", "requirements.txt", "assets/icon.ico", "installer/x.iss",
                                  "scripts/build_exe.py"])
def test_files_that_change_what_users_run_do_trigger_a_release(repo, path):
    repo.git("tag", "v1.4.5")
    repo.write(path, "changed\n")
    repo.commit("change")
    assert repo.plan()["publish"] == "true"


def test_mixed_commit_with_one_app_file_releases(repo):
    repo.git("tag", "v1.4.5")
    repo.write("README.md", "docs\n")
    repo.write("updater.py", "code\n")
    repo.commit("both")
    assert repo.plan()["publish"] == "true"


def test_bumping_the_series_changes_the_version_prefix(repo):
    repo.git("tag", "v1.4.5")
    repo.write("bose_app.py", 'APP_VERSION = "1.5.0"\n')
    repo.commit("Start version series 1.5.0")
    assert repo.plan(run_number=61)["version"] == "1.5.61"


def test_an_existing_tag_is_never_overwritten(repo):
    repo.git("tag", "v1.4.57")
    repo.write("updater.py", "code\n")
    repo.commit("change")
    out = repo.plan(run_number=57)
    assert out["publish"] == "false", "re-running an already published build must not clobber the release"


def test_force_publishes_even_docs_only_changes(repo):
    repo.git("tag", "v1.4.5")
    repo.write("README.md", "docs\n")
    repo.commit("docs")
    assert repo.plan(force=True)["publish"] == "true"
