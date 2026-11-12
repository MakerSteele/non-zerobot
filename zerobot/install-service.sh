#!/bin/bash
# ═══════════════════════════════════════════════════
# Install ZeroBot as a systemd service.
# Run once:  bash install-service.sh
#
# Why: start.sh runs the server in the foreground, so it dies with the shell
# that launched it. Over an SSH or Pi Connect session that means the robot
# stops the moment the connection drops - and because the stream server sets
# allow_reuse_address, a half-dead instance can linger on port 8080 and serve
# stale frames while you think you are running new code.
# ═══════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

echo "Installing ZeroBot service for $USER_NAME in $DIR"

# Bring the environment up now so the first start does not have to wait on it.
bash "$DIR/setup.sh"

sudo tee /etc/systemd/system/zerobot.service >/dev/null <<UNIT
[Unit]
Description=ZeroBot control server
After=network-online.target pigpiod.service
Wants=network-online.target pigpiod.service
# Keep retrying after a bad deploy instead of giving up at the default
# 5-in-10s. These live in [Unit], not [Service] - systemd ignores them with a
# warning if they are in the wrong section.
StartLimitBurst=20
StartLimitIntervalSec=300

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
# Same environment prep on every boot: packages, I2C, pigpiod. Idempotent and
# fast when nothing is missing, so the robot never depends on a manual step
# having been run at some point in the past.
ExecStartPre=/bin/bash $DIR/setup.sh
TimeoutStartSec=300
ExecStart=/usr/bin/python3 $DIR/robot.py
# always, not on-failure: a clean exit(0) from an unhandled path would
# otherwise leave the robot dead with systemd reporting success.
Restart=always
RestartSec=3
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
UNIT

# ── Liveness watchdog ──
# Covers the failure systemd cannot: the process alive but not serving.
sudo tee /etc/systemd/system/zerobot-watchdog.service >/dev/null <<UNIT
[Unit]
Description=ZeroBot liveness probe
After=zerobot.service

[Service]
Type=oneshot
ExecStart=/bin/bash $DIR/healthcheck.sh
UNIT

sudo tee /etc/systemd/system/zerobot-watchdog.timer >/dev/null <<UNIT
[Unit]
Description=Probe ZeroBot every minute

[Timer]
# 90s after boot so the first probe never races setup.sh installing packages.
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=10

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable zerobot
sudo systemctl enable --now zerobot-watchdog.timer
sudo systemctl restart zerobot
sleep 3
sudo systemctl --no-pager --lines=15 status zerobot || true

cat <<'TIPS'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Installed. From now on:

    sudo systemctl restart zerobot     after deploying new code
    sudo systemctl stop zerobot        before bench work on the wiring
    journalctl -u zerobot -f           live log ([WATCHDOG], [TRIM], errors)
    journalctl -u zerobot -n 50        last 50 lines
    journalctl -u zerobot-watchdog -n 30   what the watchdog has been doing
    systemctl list-timers zerobot-watchdog.timer   when it next fires

  Starts on boot; restarts if it crashes; and a timer probes both ports
  every 60s and restarts it if it stops answering while still running.
  The watchdog waits for two failures 8s apart and will not restart more
  than once per 5 minutes.
  start.sh still works for foreground debugging - stop the service first,
  or the two will fight over ports 5000 and 8080.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPS
