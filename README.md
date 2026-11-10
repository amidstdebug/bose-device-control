# Bose Soundbar Remote

A small desktop remote (Windows and Linux) for Bose smart speakers and soundbars (built and tested with a **Soundbar 700**). Play/pause, skip, volume, mute, power, source switching and now-playing with album art, from your PC over your local network.

> Unofficial. Not affiliated with or endorsed by Bose.

## Install on Windows

1. Open the [latest release](../../releases/latest) and download **`BoseSoundbarRemote-Setup-x.y.z.exe`**.
2. Run it. No Python and no administrator rights needed. It installs for your user only.
3. If Windows says **"Windows protected your PC"**, click **More info → Run anyway**. The app isn't code-signed yet, so Windows shows this for every new unsigned program. You can check the download against `SHA256SUMS.txt`, or verify it was built by this repository's workflow with `gh attestation verify <file> --repo <owner>/<repo>`.
4. Sign in with your Bose account (email + password). The app finds your speaker by itself.

Prefer no installer? Download the **portable zip**, unzip anywhere and run `BoseSoundbarRemote.exe`.

**Updates:** the app checks GitHub at startup (switch it off in Settings) and offers new versions with one click; it downloads the installer, checks its SHA-256 against the release's `SHA256SUMS.txt`, installs over the old version and reopens.

## Install on Linux

1. Open the [latest release](../../releases/latest) and download **`BoseSoundbarRemote-x.y.z-linux-x86_64.tar.gz`**.
2. Unpack it and run the installer. No root needed: it installs for your user only (into `~/.local/opt/BoseSoundbarRemote`), adds **Bose Soundbar** to your applications menu and creates the command `bose-soundbar-remote`.

   ```bash
   tar -xzf BoseSoundbarRemote-*-linux-x86_64.tar.gz
   ```

   ```bash
   ./BoseSoundbarRemote-*/install.sh
   ```

3. Sign in with your Bose account, exactly as on Windows.

Prefer not to install? Run `BoseSoundbarRemote/BoseSoundbarRemote` straight from the unpacked folder. To remove it, run `./uninstall.sh` (add `--purge` to also delete your settings and login tokens).

What is different on Linux:

- **Updates** are announced in the app, and the button opens the release page; download the new tarball and run `install.sh` again. (The one-click update is the Windows installer.)
- **Minimize to tray** needs a desktop with a system tray (KDE, Xfce, Cinnamon, MATE, ...). Plain GNOME has none, so there the window is minimized instead of hidden.
- **Run when I sign in**, the applications-menu entry and the desktop shortcut are ordinary `.desktop` files, switched on and off in Settings.
- **Interface size** (Settings → Appearance) scales the window for high-resolution screens.
- It needs a graphical session (X11, or Wayland with XWayland, which every mainstream desktop provides) on 64-bit x86. The build targets Ubuntu 22.04's glibc, so anything of that age or newer runs it.

**Requirements:** Windows 10/11 or a desktop Linux, a Bose account **with an email + password** (accounts created through Google/Apple sign-in need a password set first via "Forgot password"), and the PC on the same network as the speaker.

## Credits

This app is only a GUI. All communication with Bose devices is done by
**[pybose](https://github.com/cavefire/pybose)** by **[cavefire](https://github.com/cavefire)**,
which reverse-engineered the Bose local API and login flow. Huge thanks for that work, none of this would exist without it.

pybose is licensed under the GPLv3, so this project is too (see [LICENSE](LICENSE)).

## Features

- Play / pause, previous / next, with album art (cross-fades between covers) and a seekable progress bar with elapsed / total time
- Live updates: the speaker pushes changes (e.g. starting Spotify Connect from your phone), with a 5 second safety poll as a backup (every 30 seconds while the window is minimized or in the tray)
- Looks after itself: renews the Bose login before it expires, reconnects after the PC wakes from sleep or the speaker restarts, and keeps retrying quietly (with growing pauses) if the speaker isn't there when the app starts
- **Source picker**: TV, Bluetooth, Spotify (per account), Chromecast and AirPlay, built from the speaker's own live source list; any input (such as a Spotify account you don't recognise) can be hidden in Settings
- Volume slider, mute, power on / off, maximum-volume limit
- Modern UI built with [customtkinter](https://github.com/TomSchimansky/CustomTkinter): dark / light / system mode and 7 accent colours
- Settings page: display name, close behaviour (minimize to taskbar / tray / exit), start minimized, always on top, remember position, keyboard volume step, manual speaker IP, optional error-message panel (off by default), update checks
- Desktop integration toggles: desktop shortcut, Start menu / applications menu entry, run at sign-in
- Keyboard shortcuts: Space play/pause, Up/Down volume, Left/Right previous/next, M mute
- Single instance: launching it again brings the running window back
- Login form with show-password, retry and log-out; errors are shown in the app and written to a log file instead of failing silently
- Finds the speaker by mDNS, with a port-scan fallback for networks that block mDNS

## Where data is stored

`%APPDATA%\BoseSoundbarRemote\` on Windows, `~/.config/BoseSoundbarRemote/` on Linux. It holds `bose_config.json` (login tokens, speaker IP and settings, **never your password**) and `bose_app.log`. Treat the config file as sensitive and don't share it; on Linux it is readable by your user only. The uninstaller offers to delete it (`uninstall.sh --purge` on Linux). The log is capped at 1 MB plus two older files.

## Troubleshooting

- **No device found:** Windows blocks mDNS on networks marked *Public*. The app falls back to scanning port 8082 on your /24 subnet. Setting the network profile to *Private* also fixes mDNS. You can also type the speaker's IP in Settings → Connection.
- **Login failed:** check the password with "Show password". Turn on Settings → Advanced → Show error messages to see what Bose's server replied, and use *Copy* when reporting an issue.
- **Speaker moved to a new IP:** Settings → Connection → *Reconnect* or *Re-scan network*.
- **A Spotify account you don't recognise:** the speaker remembers every account ever linked to it. Hide it under Settings → Sources on the main screen.

## Development

Windows:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python bose_app.py          # run from source
.venv\Scripts\python -m pytest tests      # a few minutes (GUI tests use invisible windows)
.venv\Scripts\python -m ruff check .      # lint
build.bat                                  # builds dist\BoseSoundbarRemote\BoseSoundbarRemote.exe
```

Linux (needs Python 3.11+ with Tk: `sudo apt install python3-venv python3-tk`, or your distribution's equivalent):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./run.sh                                   # run from source
xvfb-run -a sh -c 'openbox & sleep 1; .venv/bin/python -m pytest tests'   # tests on a virtual display (apt install xvfb openbox)
./build.sh                                 # builds dist/BoseSoundbarRemote/ and the .tar.gz in dist-installer/
```

### Contributing without leaking personal data

Your login tokens, speaker IP and settings live in `%APPDATA%\BoseSoundbarRemote\`, outside the project folder, and `.gitignore` blocks config files, logs, tokens, keys, `.env` files, shortcuts (they embed your user name), screenshots, editor/assistant folders and scratch directories. On top of that there is a privacy scanner, so a slip gets caught before it is public:

```bash
git config core.hooksPath .githooks          # once per clone: runs the scanner on every commit
python scripts/check_privacy.py --all        # scan everything now
```

It flags e-mail addresses, IP and MAC addresses, home directories, GUIDs and tokens, **plus your own user name and computer name, detected automatically on your machine**. Put anything else that identifies you (full name, handles) in `.privacy-deny.local` (git-ignored). CI runs the same scan on every push and pull request. Use fake values such as `user@example.com` or `192.0.2.10` in tests and docs. Finally: GitHub shows the e-mail in your commits, so consider *Settings > Emails > Keep my email addresses private* and use your `@users.noreply.github.com` address as your git e-mail.

### What the tests cover

| File | What it protects |
| --- | --- |
| `tests/test_gui.py` | The real window against a fake speaker: login, connecting, discovery fallback, live updates, now-playing, progress/seek, album-art fade, transport, volume limits, sources and hiding, every settings control, close/minimize/tray, shortcuts, update banner |
| `tests/test_pure.py` | Config, source list building, shortcuts, port scan, single-instance, image handling |
| `tests/test_resilience.py` | Running for days: login renewal, reconnecting and back-off, no logging out on a network blip, capped log and error panel, atomic settings file, slowed-down timers while hidden, clean exit |
| `tests/test_linux.py` | Linux launchers and autostart, data folder, drawn icons, the install / uninstall scripts |
| `tests/test_updater.py` | Version comparison, release parsing, checksum verification, refusal of non-GitHub hosts |
| `tests/test_pybose_contract.py` | Every pybose function and field this app relies on, so an upstream change fails CI instead of failing for users |
| `tests/test_repo_hygiene.py` | Credit to pybose, GPL licence, no secrets committed, pinned dependencies and workflow actions, installer wiring |

### CI/CD

- **CI** (`.github/workflows/ci.yml`) runs on every push and PR: lint, the privacy scan, the full suite on Windows (Python 3.11 and 3.12) and on Linux, then builds the exe, smoke-tests it (starts, shows a window, single-instance, no tracebacks), builds the installer and runs it end to end (silent install, launch, uninstall). The Linux build gets the same treatment: build, smoke test, tarball, `install.sh`, run, `uninstall.sh`.
- **Canary** (`canary.yml`) runs weekly against the *latest* dependency versions and opens an issue if pybose or anything else broke.
- **Release** (`release.yml`) follows a green CI run on `main` and publishes automatically; see below.

### Releases are automatic

Push to `main`. When CI goes green **and the commit changes the app** (`bose_app.py`, `updater.py`, `icons.py`, assets, installer, requirements, build scripts), the **Release** workflow builds exactly that commit, tests the installer end to end (silent install, launch, uninstall), writes `SHA256SUMS.txt`, attaches a build-provenance attestation and publishes a GitHub Release with the installer, the portable zip, the Linux tarball and a changelog built from the commit messages. The app then offers that update to everyone who has it installed.

- **Version numbers** are `<series>.<n>`, for example `1.4.3`. The series (`1.4`) is `APP_VERSION` in `bose_app.py`; `n` is simply the next number after the last release in that series, stamped by CI. Pushes that don't release never use a number up, and no version-bump commits are needed.
- **Docs-only, test-only and CI-plumbing changes** don't create a release.
- Put **`[skip release]`** in a commit message to skip one explicitly.
- A new minor/major number: `.\scripts\release.ps1 -Version 1.5.0 -Push` (sets the series; the pipeline releases it as `1.5.0`, then `1.5.1`, ...).
- Re-run or force one by hand from the Actions tab: *Release → Run workflow*.

## License

GPL-3.0, see [LICENSE](LICENSE). Depends on pybose (GPL-3.0, © cavefire).
