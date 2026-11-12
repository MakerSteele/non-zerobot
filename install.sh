#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Non-Zerobot one-line installer. Run on the robot (over SSH), as your
# normal user, not root:
#
#   curl -fsSL https://raw.githubusercontent.com/MakerSteele/non-zerobot/main/install.sh | bash
#
# Checks the Pi first, downloads the newest release, installs it, shows a
# QR code for your phone, then offers to reboot. Run it again any time to
# update: your tuning settings live outside ~/zerobot and are kept.
# ═══════════════════════════════════════════════════════════════════
set -u
ZIP_URL="${ZEROBOT_ZIP_URL:-https://github.com/MakerSteele/non-zerobot/releases/latest/download/zerobot.zip}"
DIR="$HOME/zerobot"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok()   { echo "  [OK]   $*"; }
warn() { echo "  [WARN] $*"; }
stop() { echo; echo "  [STOP] $*"; echo; echo "  Nothing was changed. Fix that, then run the same command again."; exit 1; }

# Read answers from the keyboard even though this script arrives through a pipe.
ask() { local a=""; if [ -r /dev/tty ]; then read -r -p "$1" a < /dev/tty; fi; echo "$a"; }

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  NON-ZEROBOT INSTALLER"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "Checking this Pi..."

# 1. Not root
[ "$(id -u)" = 0 ] && stop "Run this as your normal user, without sudo. It asks for your password when it needs it."

# 2. A Raspberry Pi
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in
  *"Raspberry Pi"*) ok "$MODEL" ;;
  *) warn "This doesn't look like a Raspberry Pi (${MODEL:-unknown}). Continuing anyway." ;;
esac

# 3. 32-bit OS (the supported one; 64-bit won't even boot on a Pi Zero W)
ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
case "$ARCH" in
  armhf) ok "Raspberry Pi OS 32-bit" ;;
  arm64) warn "This is the 64-bit OS. The robot is tested on Raspberry Pi OS Lite (32-bit). It will probably work, but if something odd happens, re-flash with the 32-bit Lite version." ;;
  *)     warn "Unexpected system type: $ARCH" ;;
esac

# 4. Internet
if curl -fsS -m 15 -o /dev/null https://github.com; then
  ok "Internet connection"
else
  stop "No internet. The install downloads the robot software and a few system packages. Check the WiFi you set in Raspberry Pi Imager."
fi

# 5. Free space (packages + software need a few hundred MB)
FREE_MB=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
if [ "${FREE_MB:-0}" -lt 500 ]; then
  stop "Only ${FREE_MB} MB free on the SD card. Needs at least 500 MB. Use a bigger card (16 GB or more)."
fi
ok "${FREE_MB} MB free"

# 6. sudo works (asks for the password once, now, not halfway through)
echo
echo "Your password is needed once, to install system parts."
sudo -v < /dev/tty || stop "sudo didn't accept the password."
ok "Password accepted"

echo
echo "Downloading the newest robot software..."
curl -fsSL -m 120 -o "$TMP/zerobot.zip" "$ZIP_URL" || stop "Download failed: $ZIP_URL"
unzip -tq "$TMP/zerobot.zip" >/dev/null 2>&1 || stop "The download is damaged. Run the command again."
unzip -p "$TMP/zerobot.zip" version.py >/dev/null 2>&1 || stop "The download isn't the robot software (no version.py inside)."
NEW=$(unzip -p "$TMP/zerobot.zip" version.py | sed -n 's/^BUILD *= *"\(.*\)".*/\1/p')
ok "Got build $NEW"

# Stop a running robot before swapping its files.
if systemctl is-active --quiet zerobot 2>/dev/null; then
  sudo systemctl stop zerobot-watchdog.timer zerobot 2>/dev/null
  ok "Stopped the running robot software"
fi

# Keep one previous copy, just in case.
if [ -d "$DIR" ]; then
  rm -rf "$DIR.old"; mv "$DIR" "$DIR.old"
  ok "Previous version kept in ~/zerobot.old"
fi
mkdir -p "$DIR" && unzip -q "$TMP/zerobot.zip" -d "$DIR" || stop "Couldn't unpack into $DIR"
sync
ok "Installed into ~/zerobot"

echo
echo "Setting up the system (this is the slow part: several minutes, longer on a Pi Zero)..."
sudo bash "$DIR/install-root-helper.sh" || stop "The permissions step failed. Copy the messages above into an issue: https://github.com/MakerSteele/non-zerobot/issues"
bash "$DIR/install-service.sh" || stop "The service step failed. Copy the messages above into an issue: https://github.com/MakerSteele/non-zerobot/issues"
sudo apt-get install -y -qq qrencode >/dev/null 2>&1 || warn "Couldn't install qrencode, so no QR code. The address is below."

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
URL="http://${IP}:5000"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DONE. After the restart, open this on your phone"
echo "  (same WiFi as the robot):"
echo
echo "     $URL"
echo "     http://$(hostname).local:5000"
echo
if command -v qrencode >/dev/null 2>&1 && [ -n "$IP" ]; then
  echo "  Or point your phone's camera at this:"
  qrencode -t ANSIUTF8 -m 2 "$URL"
fi
echo "  First time? Open the ⚙ menu and tap CHECK. It walks"
echo "  you through anything that isn't wired right yet."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

ANS=$(ask "Restart the robot now? [Y/n] ")
case "$ANS" in
  n|N|no|NO) echo "OK. Restart later with: sudo reboot" ;;
  *) echo "Restarting..."; sudo reboot ;;
esac
