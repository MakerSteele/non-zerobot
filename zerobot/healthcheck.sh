#!/bin/bash
# ═══════════════════════════════════════════════════
# ZeroBot liveness probe. Run every 60s by zerobot-watchdog.timer.
#
# systemd's Restart= only sees a process that EXITS. It cannot see one that is
# still running but wedged - a deadlocked camera thread, a hung socket, a
# Python thread that died and took the request handler with it. To systemd
# that process looks perfectly healthy. This script asks the server to prove
# it by answering, which is the only test that means anything.
#
# Deliberately conservative: two failed probes 8s apart before acting, and no
# more than one restart per 5 minutes. A watchdog that restarts on a single
# blip, or that loops on a genuinely broken build, is worse than none.
# ═══════════════════════════════════════════════════
set -u

CONTROL="http://127.0.0.1:5000/version"
STREAM="http://127.0.0.1:8080/health"
STATE="/run/zerobot-watchdog.last"
MIN_GAP=300          # seconds between permitted restarts
TIMEOUT=5

probe() {
  # /stallcheck and /paircheck deliberately block for up to 15s, but Flask runs
  # threaded=True so /version still answers while they do. A timeout here means
  # the server is genuinely not serving, not merely busy.
  curl -fs --max-time "$TIMEOUT" "$CONTROL" >/dev/null 2>&1 || return 1
  curl -fs --max-time "$TIMEOUT" "$STREAM"  >/dev/null 2>&1 || return 1
  return 0
}

if probe; then
  exit 0
fi

echo "zerobot-watchdog: first probe failed, retrying in 8s"
sleep 8

if probe; then
  echo "zerobot-watchdog: recovered on retry, no action taken"
  exit 0
fi

now=$(date +%s)
last=0
[ -f "$STATE" ] && last=$(cat "$STATE" 2>/dev/null || echo 0)
gap=$(( now - last ))

if [ "$gap" -lt "$MIN_GAP" ]; then
  echo "zerobot-watchdog: still unhealthy but last restart was ${gap}s ago (<${MIN_GAP}s) - holding off."
  echo "zerobot-watchdog: repeated holds mean the code itself is broken; check journalctl -u zerobot"
  exit 0
fi

echo "$now" > "$STATE"
echo "zerobot-watchdog: unresponsive on both ports - restarting zerobot"
systemctl restart zerobot
