#!/bin/bash
# Install (or remove with "uninstall") the launchd service that keeps the web app running.
# Usage: scripts/install_webapp_service.sh [uninstall]
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="dev.jcmonteiro.manga-kindle.webapp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$1" = "uninstall" ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Web app service removed."
    exit 0
fi

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
mkdir -p logs "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$PY</string><string>$ROOT/webapp.py</string></array>
    <key>WorkingDirectory</key><string>$ROOT</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$ROOT/logs/webapp.log</string>
    <key>StandardErrorPath</key><string>$ROOT/logs/webapp.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Web app running at http://127.0.0.1:8766 (starts at login, restarts if it crashes)."
