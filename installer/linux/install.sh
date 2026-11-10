#!/bin/sh
# Installs Bose Soundbar Remote for the current user only. No root needed; run again to upgrade.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
src="$here/BoseSoundbarRemote"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
app_dir="${BOSE_REMOTE_PREFIX:-$HOME/.local/opt}/BoseSoundbarRemote"
bin_dir="$HOME/.local/bin"
desktop_file="$data_home/applications/bose-soundbar-remote.desktop"
icon_file="$data_home/icons/hicolor/256x256/apps/bose-soundbar-remote.png"

if [ "$(id -u)" = "0" ] && [ -z "${BOSE_REMOTE_ALLOW_ROOT:-}" ]; then
    echo "Run this as your normal user, not with sudo: it installs into your home folder." >&2
    exit 1
fi
if [ ! -x "$src/BoseSoundbarRemote" ]; then
    echo "Can't find $src/BoseSoundbarRemote. Run install.sh from the unpacked download." >&2
    exit 1
fi

# a running copy would keep the old files open; ask it to stop (it saves its settings as it goes)
if command -v pkill >/dev/null 2>&1; then
    pkill -u "$(id -u)" -x BoseSoundbarRemote 2>/dev/null || true
    sleep 1
fi

mkdir -p "$(dirname "$app_dir")" "$bin_dir" "$(dirname "$desktop_file")" "$(dirname "$icon_file")"
rm -rf "$app_dir.new"
cp -a "$src" "$app_dir.new"
rm -rf "$app_dir"
mv "$app_dir.new" "$app_dir"
cp "$here/icon.png" "$icon_file"
ln -sf "$app_dir/BoseSoundbarRemote" "$bin_dir/bose-soundbar-remote"

cat > "$desktop_file" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Bose Soundbar
GenericName=Speaker remote
Comment=Remote control for Bose speakers and soundbars
Exec="$app_dir/BoseSoundbarRemote"
Path=$app_dir
Icon=$icon_file
Terminal=false
Categories=AudioVideo;Audio;
StartupWMClass=BoseSoundbarRemote
StartupNotify=false
DESKTOP
chmod 755 "$desktop_file"

# an autostart entry from an earlier install points at the same place; nothing to rewrite there
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data_home/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$data_home/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed to $app_dir"
echo "Start it from your applications menu (Bose Soundbar), or run: $bin_dir/bose-soundbar-remote"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Note: $bin_dir is not on your PATH, so use the full path above or the menu entry." ;;
esac
