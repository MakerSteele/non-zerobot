#!/usr/bin/env bash
# ZeroBot access point setup - run ONCE on the Pi, with a screen attached.
# Creates NetworkManager profile "zerobot-ap": the Pi hosts its own WiFi,
# the phone joins it, the Pi is always 10.42.0.1.
#
# Usage:  sudo bash setup-ap.sh       (from inside ~/zerobot)
# Then:   sudo netmode ap              (bring the AP up now)
#
# Safe to re-run: deletes and recreates the profile.

set -euo pipefail

SSID="ZeroBot"
PSK="zerobot1"          # 8+ chars. Change it here before running if you like.
CON="zerobot-ap"
ADDR="10.42.0.1/24"
CHANNEL=6

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "== removing old $CON profile (if any)"
nmcli connection delete "$CON" >/dev/null 2>&1 || true

echo "== creating $CON"
nmcli connection add type wifi ifname wlan0 con-name "$CON" \
    autoconnect no \
    ssid "$SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel "$CHANNEL" \
    ipv4.method shared \
    ipv4.addresses "$ADDR" \
    ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK" \
    wifi-sec.psk-flags 0

# The 14 Sep lesson: 'add' can succeed without storing the PSK. Verify.
echo "== verifying stored settings"
nmcli -s -g 802-11-wireless.mode,802-11-wireless.band,ipv4.method,ipv4.addresses,wifi-sec.psk \
    connection show "$CON"

STORED=$(nmcli -s -g wifi-sec.psk connection show "$CON")
[ "$STORED" = "$PSK" ] || { echo "!! PSK did not store - aborting"; exit 1; }

echo
echo "== installing netmode command"
install -m 755 "$(dirname "$(readlink -f "$0")")/netmode.sh" /usr/local/bin/netmode

echo
echo "OK. Profile '$CON' created (autoconnect off - netmode controls it)."
echo "Next:  sudo netmode ap"
