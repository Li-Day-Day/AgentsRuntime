#!/usr/bin/env bash
set -euo pipefail

ulimit -c 0

export DISPLAY="${DISPLAY:-:1}"
export QT_QPA_PLATFORM=xcb
export XDG_CURRENT_DESKTOP=KDE
export XDG_SESSION_DESKTOP=KDE
export XDG_SESSION_TYPE=x11
export KDE_SESSION_VERSION=6

mkdir -p \
    "$HOME/.config" \
    "$HOME/.config/autostart" \
    "$HOME/.XDG" \
    "$HOME/.local/share"
chmod 0700 "$HOME/.XDG"
touch "$HOME/.local/share/user-places.xbel"
sudo rm -f /usr/share/dbus-1/system-services/org.freedesktop.UDisks2.service

if [[ ! -f "$HOME/.config/kwinrc" ]]; then
    kwriteconfig6 --file "$HOME/.config/kwinrc" \
      --group Compositing --key Enabled false
fi
if [[ ! -f "$HOME/.config/kscreenlockerrc" ]]; then
    kwriteconfig6 --file "$HOME/.config/kscreenlockerrc" \
      --group Daemon --key Autolock false
fi

kbuildsycoca6 || true

exec dbus-run-session bash -c '
  set -e
  kwin_x11 --replace &
  kwin_pid=$!

  cleanup() {
    kill "$kwin_pid" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  for _ in $(seq 1 40); do
    if qdbus6 org.kde.KWin /KWin supportInformation >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  if [[ -x /usr/lib/libexec/polkit-kde-authentication-agent-1 ]]; then
    /usr/lib/libexec/polkit-kde-authentication-agent-1 &
  fi

  plasmashell
'
