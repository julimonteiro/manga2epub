#!/bin/bash
# Install (or remove with "uninstall") the daily launchd job.
# Usage: scripts/install_daily_job.sh [HH:MM]   (default 08:00)
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="dev.jcmonteiro.manga-kindle.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$1" = "uninstall" ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Daily job removed."
    exit 0
fi

TIME="${1:-08:00}"
HOUR=$((10#${TIME%%:*}))
MINUTE=$((10#${TIME##*:}))

mkdir -p logs "$HOME/Library/LaunchAgents"
chmod +x scripts/daily_job.sh

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$ROOT/scripts/daily_job.sh</string></array>
    <key>WorkingDirectory</key><string>$ROOT</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$ROOT/logs/daily.log</string>
    <key>StandardErrorPath</key><string>$ROOT/logs/daily.log</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Daily job installed: every day at $TIME and at every login (log: $ROOT/logs/daily.log)."
