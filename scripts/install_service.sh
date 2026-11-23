#!/bin/bash
# Run get.ekyc (and its Cloudflare tunnel) as macOS startup services.
#
#   ./scripts/install_service.sh              install / reinstall
#   ./scripts/install_service.sh --uninstall  remove (back to starting it by hand)
#
# Both start at boot, before anyone logs in, run as you (not root), and are
# restarted by macOS if they ever stop. Asks for your Mac password once (sudo).
set -euo pipefail

[ "$(uname)" = Darwin ] || { echo "This is for macOS (the Mac mini)."; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run it as yourself, not with sudo: it asks for the password itself."; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ME="$(id -un)"
HOMEDIR="$HOME"
APP=my.getride.ekyc
TUN=my.getride.ekyc.tunnel
DIR=/Library/LaunchDaemons
TOKEN="$HOMEDIR/.cloudflared/ekyc.token"
BREW="$(/opt/homebrew/bin/brew --prefix 2>/dev/null || /usr/local/bin/brew --prefix 2>/dev/null || echo /opt/homebrew)"
PATHS="$BREW/bin:$BREW/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

stop() {  # $1 label
  sudo launchctl bootout "system/$1" 2>/dev/null || true
  sudo rm -f "$DIR/$1.plist"
}

if [ "${1:-}" = "--uninstall" ]; then
  sudo -v
  stop "$APP"; stop "$TUN"
  echo "Removed. Start the server by hand again (see the README)."
  exit 0
fi

[ -x "$REPO/.venv/bin/uvicorn" ] || { echo "No $REPO/.venv/bin/uvicorn: set up the virtualenv first (README, Quick start)."; exit 1; }
[ -f "$REPO/.env" ] || { echo "No $REPO/.env: create it first (README, Quick start)."; exit 1; }

echo "Asking for your Mac password to install the startup services…"
sudo -v

# Anything started by hand would hold port 8080 / run a second tunnel.
pkill -f "uvicorn app.main" 2>/dev/null || true
pkill -f "cloudflared tunnel run" 2>/dev/null || true
sleep 1

install_plist() {  # $1 label, $2 plist body
  local tmp; tmp="$(mktemp)"
  printf '%s\n' "$2" > "$tmp"
  plutil -lint "$tmp" >/dev/null
  stop "$1"
  sudo install -o root -g wheel -m 644 "$tmp" "$DIR/$1.plist"
  rm -f "$tmp"
  sudo launchctl bootstrap system "$DIR/$1.plist"
}

install_plist "$APP" "<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>$APP</string>
  <key>UserName</key><string>$ME</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
    <string>$REPO/.venv/bin/uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8080</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATHS</string>
    <key>HOME</key><string>$HOMEDIR</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$REPO/server.log</string>
  <key>StandardErrorPath</key><string>$REPO/server.log</string>
</dict></plist>"
echo "✓ get.ekyc server installed as a startup service ($APP)"

if [ -f "$DIR/com.cloudflare.cloudflared.plist" ]; then
  echo "• Tunnel: already a service (installed with 'cloudflared service install'); left as it is."
elif [ -f "$TOKEN" ] && command -v "$BREW/bin/cloudflared" >/dev/null; then
  # The token stays in its file (mode 600); it is never written into the plist.
  install_plist "$TUN" "<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>$TUN</string>
  <key>UserName</key><string>$ME</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>TUNNEL_TOKEN=\"\$(cat '$TOKEN')\" exec '$BREW/bin/cloudflared' tunnel run</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATHS</string>
    <key>HOME</key><string>$HOMEDIR</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOMEDIR/.cloudflared/tunnel.log</string>
  <key>StandardErrorPath</key><string>$HOMEDIR/.cloudflared/tunnel.log</string>
</dict></plist>"
  echo "✓ Cloudflare tunnel installed as a startup service ($TUN)"
else
  echo "• Tunnel: not installed (no $TOKEN or no cloudflared). Save the token there and run this again."
fi

# A server should come back after a power cut and not go to sleep.
sudo pmset -a autorestart 1 sleep 0 >/dev/null && echo "✓ Mac set to restart after a power failure and never sleep (display sleep unchanged)"
if fdesetup isactive >/dev/null 2>&1; then
  echo "! FileVault is on: after a reboot the Mac waits for your password at the login screen before anything starts."
  echo "  To let it start unattended, turn FileVault off (System Settings > Privacy & Security), or log in after each reboot."
fi

printf "Waiting for the server"
for _ in $(seq 1 30); do
  if curl -fs localhost:8080/health >/dev/null; then
    echo; curl -s localhost:8080/health; echo
    echo "✓ Running. It now starts by itself at boot and restarts if it stops."
    exit 0
  fi
  printf "."; sleep 2
done
echo; echo "✗ The server has not answered after 60 s. Last lines of $REPO/server.log:"; tail -20 "$REPO/server.log"
exit 1
