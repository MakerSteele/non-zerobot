#!/bin/bash
# ═══════════════════════════════════════════════════
# ZeroBot Startup Script
# Run this to start the robot: bash start.sh
# ═══════════════════════════════════════════════════

# Go to the robot directory
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ZEROBOT STARTING"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Install dependencies if not already installed.
# apt is more reliable than pip on current Pi OS releases; pip is the fallback.
echo "[1/3] Preparing environment..."
bash "$(dirname "$0")/setup.sh"

echo "[2/3] Environment ready"
echo "[3/3] Starting robot server..."
echo ""
echo "  Control page:  http://$(hostname -I | awk '{print $1}'):5000"
echo "  Camera stream: http://$(hostname -I | awk '{print $1}'):8080/stream"
echo ""
echo "  Press Ctrl+C to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Refuse to start on top of a running service. Two instances fight over ports
# 5000 and 8080, and because the stream server sets allow_reuse_address the
# second one can bind anyway - leaving you debugging frames served by the old
# process. This is exactly how "the camera is still upside down" happens after
# a fix that was never actually running.
if systemctl is-active --quiet zerobot 2>/dev/null; then
  echo ""
  echo "[STOP] The zerobot service is already running."
  echo "       Foreground debugging:  sudo systemctl stop zerobot && bash start.sh"
  echo "       Just restart it:       sudo systemctl restart zerobot"
  echo "       Watch its log:         journalctl -u zerobot -f"
  exit 1
fi

if pgrep -f "python3 .*robot.py" >/dev/null 2>&1; then
  echo ""
  echo "[STOP] Another robot.py is already running (pid $(pgrep -f 'python3 .*robot.py' | head -1))."
  echo "       Clear it with: pkill -f robot.py"
  exit 1
fi

# Start the Flask server
python3 robot.py
