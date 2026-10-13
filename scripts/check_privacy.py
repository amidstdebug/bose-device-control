"""Privacy guard: fails if files about to be committed (or already tracked) contain personal data.

    python scripts/check_privacy.py --staged     # what `git commit` is about to record (used by the pre-commit hook)
    python scripts/check_privacy.py --all        # every tracked + not-ignored file (used by CI and the tests)
    python scripts/check_privacy.py FILE...      # specific files

It needs no knowledge of *who you are* in the repository, so anyone who forks the project is protected:

1. Generic patterns: e-mail addresses, private/public IP addresses, MAC addresses, Windows/macOS/Linux home
   directories (which contain a user name), GUIDs, and tokens/keys.
2. This machine's own identity, derived at run time: the OS user name, computer name and home directory.
3. Anything you list in `.privacy-deny.local` (git-ignored; one string per line, e.g. your name or e-mail).

Harmless values (documentation addresses, the fake data in the tests) are listed in `.privacy-allow`.
Matches are shown masked so the report itself doesn't leak what it found.
"""
import getpass
import ipaddress
import re
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
HOME_DIR = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)([^\\/\s\"'<>|*?{}%$]+)", re.IGNORECASE)  # privacy: ignore
GUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
SECRETS = {
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]*"),
    "stored token": re.compile(r"(?:access|refresh|id)_token[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._~+/=-]{30,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{30,}"),
}
HARMLESS_HOME_NAMES = {"public", "default", "all users", "default user", "user", "username", "name", "you", "me",
                       "runner", "runneradmin", "<user>", "your-name", "yourname"}
HARMLESS_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com", "noreply.github.com",
                          "bose.com", "localhost")
DOC_NETWORKS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
SKIP_NAMES = {".privacy-allow", ".privacy-deny.local"}


def mask(text):
    return text if len(text) <= 6 else f"{text[:3]}…{text[-2:]} ({len(text)} chars)"


def load_lines(path):
    try:
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        return []


def machine_identity():
    """Strings that identify the person running the check: user name, computer name, home directory."""
    names = set()
    for getter in (getpass.getuser, socket.gethostname):
        try:
            names.add(getter())
        except Exception:
            pass
    names.add(Path.home().name)
    return {n for n in names if len(n) >= 4 and n.lower() not in HARMLESS_HOME_NAMES}


def personal_terms(root=ROOT):
    terms = machine_identity() | set(load_lines(root / ".privacy-deny.local"))
    return [t for t in terms if len(t) >= 3]


def is_harmless_ip(text):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an IP at all (e.g. a version number like 300.1.2.3)
    return ip.is_loopback or ip.is_unspecified or ip.is_multicast or any(ip in n for n in DOC_NETWORKS)


def scan_text(text, allow=frozenset(), terms=()):
    """Returns [(line_number, kind, matched_text)] for one file's text."""
    findings = []
    term_rx = [(t, re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.IGNORECASE)) for t in terms]
    for n, line in enumerate(text.splitlines(), 1):
        if "privacy: ignore" in line:  # opt-out for lines that only *look* personal (regexes, test data)
            continue
        for m in EMAIL.finditer(line):
            domain = m.group(0).rsplit("@", 1)[1].lower()
            if not any(domain == d or domain.endswith("." + d) for d in HARMLESS_EMAIL_DOMAINS):
                findings.append((n, "e-mail address", m.group(0)))
        for m in IPV4.finditer(line):
            if not is_harmless_ip(m.group(0)):
                findings.append((n, "IP address", m.group(0)))
        for m in MAC.finditer(line):
            findings.append((n, "MAC address", m.group(0)))
        for m in HOME_DIR.finditer(line):
            if m.group(1).lower() not in HARMLESS_HOME_NAMES:
                findings.append((n, "home directory (contains a user name)", m.group(0)))
        for m in GUID.finditer(line):
            findings.append((n, "GUID (device / account id)", m.group(0)))
        for kind, rx in SECRETS.items():
            for m in rx.finditer(line):
                findings.append((n, kind, m.group(0)))
        for term, rx in term_rx:
            if rx.search(line):
                findings.append((n, "your personal identifier", term))
    return [f for f in findings if f[2] not in allow and f[2].lower() not in {a.lower() for a in allow}]


def is_text(data):
    return b"\0" not in data[:8000]


def git(*args, root=ROOT):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def tracked_and_untracked(root=ROOT):
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", root=root)
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def staged_files(root=ROOT):
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", root=root)
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def scan_paths(paths, root=ROOT, staged=False):
    allow_lines = load_lines(root / ".privacy-allow")
    skip_files = {ln[5:].strip().replace("\\", "/") for ln in allow_lines if ln.startswith("file:")}
    allow = {ln for ln in allow_lines if not ln.startswith("file:")}
    terms = personal_terms(root)
    results = {}
    for rel in paths:
        if Path(rel).name in SKIP_NAMES or rel.replace("\\", "/") in skip_files:
            continue
        try:
            data = git("show", f":{rel}", root=root) if staged else (root / rel).read_bytes()
        except (subprocess.CalledProcessError, OSError):
            continue
        if len(data) > MAX_BYTES or not is_text(data):
            continue
        found = scan_text(data.decode("utf-8", "replace"), allow, terms)
        if found:
            results[rel] = found
    return results


def report(results):
    for rel, findings in sorted(results.items()):
        for n, kind, text in findings:
            print(f"  {rel}:{n}: {kind}: {mask(text)}")


def warn_about_identity(root=ROOT):
    """Commit authorship is public once pushed; nudge people to GitHub's no-reply address (non-blocking)."""
    try:
        email = git("config", "user.email", root=root).decode().strip()
    except subprocess.CalledProcessError:
        return
    if email and "noreply" not in email.lower():
        print("note: your commits are signed with a real e-mail address, which becomes public once pushed.\n"
              "      GitHub offers a private one (Settings > Emails > 'Keep my email addresses private').")


def main(argv):
    staged = "--staged" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        paths = staged_files() if staged else tracked_and_untracked()
    results = scan_paths(paths, staged=staged and not [a for a in argv if not a.startswith("--")])
    if results:
        print("PRIVACY CHECK FAILED: personal or sensitive data found:\n")
        report(results)
        print("\nRemove it, replace it with a fake value (e.g. user@example.com, 192.0.2.10), or, if it is genuinely "
              "harmless, add the exact value to .privacy-allow.")
        return 1
    if staged:
        warn_about_identity()
    print(f"privacy check passed ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
