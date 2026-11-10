#!/bin/sh
# Removes Bose Soundbar Remote for the current user. Add --purge to also delete settings and login tokens.
set -eu

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
app_dir="${BOSE_REMOTE_PREFIX:-$HOME/.local/opt}/BoseSoundbarRemote"

if command -v pkill >/dev/null 2>&1; then
    pkill -u "$(id -u)" -x BoseSoundbarRemote 2>/dev/null || true
    sleep 1
fi

rm -rf "$app_dir"
rm -f "$HOME/.local/bin/bose-soundbar-remote" \
      "$data_home/applications/bose-soundbar-remote.desktop" \
      "$data_home/icons/hicolor/256x256/apps/bose-soundbar-remote.png" \
      "$config_home/autostart/bose-soundbar-remote.desktop"
desktop_dir=$( (command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP) || echo "$HOME/Desktop")
rm -f "$desktop_dir/bose-soundbar-remote.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data_home/applications" >/dev/null 2>&1 || true

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$config_home/BoseSoundbarRemote"
    echo "Removed the app and its settings."
else
    echo "Removed the app. Your settings are still in $config_home/BoseSoundbarRemote (use --purge to delete them)."
fi
