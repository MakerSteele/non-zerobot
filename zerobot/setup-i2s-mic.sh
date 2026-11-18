#!/bin/bash
# setup-i2s-mic.sh - ONE-TIME setup for the SPH0645 I2S mic (13k, overlay changed 13l).
#
#     sudo bash ~/zerobot/setup-i2s-mic.sh      then reboot
#
# Only for a robot that HAS the I2S mic. Wiring (Pi pin numbers):
#   3V -> 17   GND -> 20   BCLK -> 12 (GPIO 18)   LRCL -> 35 (GPIO 19)
#   DOUT -> 38 (GPIO 20)   SEL -> 34 (GND = left channel)
#   Pin 35 is the right encoder's H2 (wired, never read): REMOVE that jumper.
#
# What it does (all of it was done by hand on 17 Nov 2026 and proven):
#   1. boot config: dtparam=i2s=on, dtparam=audio=off,
#      dtoverlay=adau7002-simple   (13l - claims no GPIO pins. The 13k
#      googlevoicehat-soundcard overlay grabbed GPIO 16 = RGB headlight green;
#      this script comments that line out if it finds it)
#   2. pigpiod starts with "-t 0" (paces with PWM, not PCM) - systemd drop-in
#   3. checks the kernel: 6.18.50 recorded garbage at instant speed, 6.18.60
#      works. Offers the command to update.
# The root helper must be current too (pcm hand-over): install-root-helper.sh.
# Undo: delete the three config lines (adau7002-simple etc.) and
#       /etc/systemd/system/pigpiod.service.d/i2s.conf, then reboot.
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo:  sudo bash $0"; exit 1; }
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt

add_line() {   # add_line <line>  - only if not already there (uncommented)
  grep -qxF "$1" "$CFG" || { echo "$1" >> "$CFG"; echo "[DONE] added to $CFG: $1"; }
}
if grep -qx "dtparam=audio=on" "$CFG"; then
  sed -i 's/^dtparam=audio=on$/dtparam=audio=off/' "$CFG"
  echo "[DONE] $CFG: dtparam=audio=on -> off (the headphone jack shares the PWM pacing)"
fi
add_line "dtparam=audio=off"
add_line "dtparam=i2s=on"
if grep -qx "dtoverlay=googlevoicehat-soundcard" "$CFG"; then
  sed -i 's/^dtoverlay=googlevoicehat-soundcard$/#dtoverlay=googlevoicehat-soundcard  # 13l: held GPIO 16 (RGB green)/' "$CFG"
  echo "[DONE] $CFG: googlevoicehat-soundcard commented out (it held GPIO 16 = RGB headlight green)"
fi
add_line "dtoverlay=adau7002-simple"

D=/etc/systemd/system/pigpiod.service.d
mkdir -p "$D"
printf '[Service]\nExecStart=\nExecStart=/usr/bin/pigpiod -l -t 0\n' > "$D/i2s.conf"
systemctl daemon-reload
echo "[DONE] pigpiod will start with -t 0"

K=$(uname -r | cut -d+ -f1 | cut -d- -f1)
if printf '%s\n%s\n' "6.18.60" "$K" | sort -V -C; then
  echo "[OK]   kernel $K"
else
  echo "[WARN] kernel $K is older than 6.18.60 - recordings come back empty on 6.18.50."
  echo "       Update:  sudo cp /boot/firmware/kernel7.img /boot/firmware/kernel7-old.img && sudo apt update && sudo apt install -y linux-image-rpi-v7"
fi

if [ -x /usr/local/sbin/zerobot-root ] && cmp -s "$(dirname "$0")/pcm_swap.py" /usr/local/lib/zerobot/pcm_swap.py; then
  echo "[OK]   root helper has the pcm hand-over"
else
  echo "[WARN] root helper needs updating:  sudo bash ~/zerobot/install-root-helper.sh"
fi
echo "== Reboot to finish:  sudo reboot"
