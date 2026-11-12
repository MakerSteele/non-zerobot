#!/usr/bin/env bash
# ZeroBot network mode switch - safe to run over SSH, no screen needed.
#
#   sudo netmode ap               Pi hosts "ZeroBot" WiFi at 10.42.0.1
#   sudo netmode client        Pi joins office/hotspot/home as before
#   sudo netmode status
#
# Your SSH session WILL drop when wlan0 flips. That is expected. The switch
# itself runs detached under systemd so the drop cannot kill it half way.
#
# Dead-man's switch: after 'ap', if no device has joined the AP within
# 5 minutes, the Pi reverts to client mode by itself. Turn the hotspot on
# and it comes back the old way. You are never locked out.
#
# Client profiles (office/hotspot/home) always keep autoconnect=yes, so if
# the AP profile ever fails to activate, NetworkManager falls through to them.

set -euo pipefail
AP="zerobot-ap"
LOG=/var/log/netmode.log
MODE="${1:-status}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

status() {
    echo "-- active:";  nmcli -t -f NAME,DEVICE connection show --active | grep wlan0 || echo "   (nothing on wlan0)"
    echo "-- address:"; hostname -I
    echo "-- AP autoconnect: $(nmcli -g connection.autoconnect connection show "$AP" 2>/dev/null || echo missing)"
    echo "-- stations joined: $(iw dev wlan0 station dump 2>/dev/null | grep -c '^Station' || true)"
    [ -f "$LOG" ] && { echo "-- last log lines:"; tail -n 5 "$LOG"; }
}

# Re-launch detached so the SSH drop can't kill us mid-switch.
if [ "${NETMODE_DETACHED:-}" != "1" ] && [ "$MODE" != "status" ]; then
    systemctl stop netmode-deadman.timer netmode-deadman.service 2>/dev/null || true
    NETMODE_DETACHED=1 systemd-run --unit=netmode-switch --quiet --collect \
        --setenv=NETMODE_DETACHED=1 bash "$(readlink -f "$0")" "$MODE"
    echo "Switching to '$MODE' in the background. SSH will drop in a moment."
    [ "$MODE" = "ap" ] && echo "Join WiFi 'ZeroBot', then: ssh ${SUDO_USER:-$USER}@10.42.0.1  /  http://10.42.0.1:5000"
    exit 0
fi

echo "$(date '+%F %T') mode=$MODE" >> "$LOG"

case "$MODE" in
  ap)
    nmcli connection modify "$AP" connection.autoconnect yes connection.autoconnect-priority 50
    # Dead-man: revert to client if nobody joins within 5 min.
    systemd-run --unit=netmode-deadman --quiet --collect --on-active=300 \
        bash -c "if [ \"\$(iw dev wlan0 station dump | grep -c '^Station')\" = 0 ]; then \
                   echo \"\$(date '+%F %T') deadman: no station, reverting\" >> $LOG; \
                   NETMODE_DETACHED=1 bash $(readlink -f "$0") client; \
                 else echo \"\$(date '+%F %T') deadman: station present, AP kept\" >> $LOG; fi"
    nmcli connection up "$AP" >> "$LOG" 2>&1
    ;;
  client)
    systemctl stop netmode-deadman.timer 2>/dev/null || true
    nmcli connection modify "$AP" connection.autoconnect no
    nmcli connection down "$AP" >> "$LOG" 2>&1 || true
    # NM now autoconnects office/hotspot/home by their own priorities.
    ;;
  status) ;;
  *) echo "usage: $0 ap|client|status"; exit 1 ;;
esac
status >> "$LOG" 2>&1
