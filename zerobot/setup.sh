#!/bin/bash
# ═══════════════════════════════════════════════════
# ZeroBot environment setup — idempotent, safe to run every boot.
#
# Everything the robot needs from the OS lives here: packages, kernel
# interfaces, daemons. Nothing should ever have to be done by hand before
# starting the program. Called by start.sh and by the systemd unit's
# ExecStartPre, so the service gets the same treatment on every boot.
#
# Cheap checks come first, so a healthy system falls through in a second or two.
# ═══════════════════════════════════════════════════
cd "$(dirname "$0")"

# ── Root jobs (11e) ──────────────────────────────────────────────
# The service has no terminal, so plain `sudo` can't ask for a password and
# every sudo below used to fail silently. Root jobs now go through one
# root-owned helper with a fixed list of allowed jobs - see zerobot-root.sh.
# One-time install:  sudo bash ~/zerobot/install-root-helper.sh
ZB_ROOT=/usr/local/sbin/zerobot-root
if sudo -n "$ZB_ROOT" check >/dev/null 2>&1; then
  ZB_ROOT_OK=1
  cmp -s ./zerobot-root.sh "$ZB_ROOT" && cmp -s ./powersave.sh /usr/local/lib/zerobot/powersave.sh \
    && cmp -s ./pcm_swap.py /usr/local/lib/zerobot/pcm_swap.py \
    || echo "[WARN] root helper is out of date - update once:  sudo bash ~/zerobot/install-root-helper.sh"
else
  ZB_ROOT_OK=0
  echo "[WARN] root helper not installed - power saving and package installs can't run."
  echo "       Fix once:  sudo bash ~/zerobot/install-root-helper.sh"
fi
asroot() {      # asroot <job> [args] - see zerobot-root.sh for the allowed jobs
  [ "$ZB_ROOT_OK" = 1 ] || { echo "[WARN] needs root (helper missing): $*"; return 1; }
  sudo -n "$ZB_ROOT" "$@"
}

# ── Python packages ──────────────────────────────────────────────
python3 -c "import flask" 2>/dev/null || \
  asroot apt python3-flask || pip3 install flask --quiet --break-system-packages

python3 -c "import gpiozero" 2>/dev/null || \
  asroot apt python3-gpiozero || pip3 install gpiozero --quiet --break-system-packages

# apt ONLY. pip's picamera2 on ARM does not bring the system libcamera
# bindings and will not work.
python3 -c "import picamera2" 2>/dev/null || \
  asroot apt python3-picamera2 || \
  echo "[WARN] python3-picamera2 install failed - robot will run without video"

# ── I2C, for the ADS1115 battery readout ─────────────────────────
# Enabling writes dtparam to config.txt AND applies it live, so no reboot is
# needed in the normal case. /dev/i2c-1 is the thing that actually matters.
if [ ! -e /dev/i2c-1 ]; then
  echo "[SETUP] I2C is off - enabling it"
  asroot i2c
  for _ in 1 2 3 4 5; do
    [ -e /dev/i2c-1 ] && break
    sleep 1
  done
  if [ -e /dev/i2c-1 ]; then
    echo "[OK] I2C enabled - no reboot needed"
  else
    echo "[WARN] I2C enabled in config but /dev/i2c-1 has not appeared."
    echo "       It will be there after the next reboot; battery reads N/A until then."
  fi
fi

command -v i2cdetect >/dev/null 2>&1 || asroot apt i2c-tools >/dev/null 2>&1

# ADS1115 needs the driver AND Blinka, which supplies `board` and `busio`.
if ! python3 -c "import board, busio, adafruit_ads1x15.ads1115" 2>/dev/null; then
  echo "[SETUP] Installing ADS1115 support (pulls in Blinka, takes a minute)"
  pip3 install adafruit-circuitpython-ads1x15 --break-system-packages \
    || echo "[WARN] ADS1115 library install failed - battery will read N/A"
fi

# ── pigpio, for encoder edge counting ────────────────────────────
if ! pgrep -x pigpiod >/dev/null 2>&1; then
  command -v pigpiod >/dev/null 2>&1 || asroot apt pigpio python3-pigpio
  asroot pigpiod
  sleep 1
  pgrep -x pigpiod >/dev/null 2>&1 \
    && echo "[OK] pigpiod running" \
    || echo "[WARN] pigpiod unavailable - encoders off, robot will pull to one side"
fi

# ── Voice: text-to-speech + certificate for the TALK page ───────
# espeak-ng gives the robot its "say" voice. The TALK page needs HTTPS for
# the phone's microphone, so make a self-signed certificate once. It lives
# OUTSIDE ~/zerobot so a redeploy doesn't replace it - otherwise the phone
# would warn about a new certificate after every update.
command -v espeak-ng >/dev/null 2>&1 || asroot apt espeak-ng \
  || echo "[WARN] espeak-ng install failed - SAY won't work, live talk and clips still will"

# scipy runs the voice filters in C (several times less CPU than Python
# loops). Optional: voice.py falls back to the loops without it.
python3 -c "import scipy.signal" 2>/dev/null || asroot apt python3-scipy \
  || echo "[WARN] python3-scipy install failed - voice works, just uses more CPU"

# Low-battery cutoff and the Shutdown button need to run shutdown as root
# without a password (the service has no terminal to type one into).
# One-time rule, installed by hand - see the 2026-11-11a notes.
if ! sudo -n -l /usr/sbin/shutdown >/dev/null 2>&1; then
  echo "[WARN] No permission to shut down - low-battery cutoff and the Shutdown button won't work."
  echo "       Fix once:  echo '$(whoami) ALL=(root) NOPASSWD: /usr/sbin/shutdown' | sudo tee /etc/sudoers.d/zerobot-shutdown && sudo chmod 440 /etc/sudoers.d/zerobot-shutdown"
fi

TLS_DIR="$HOME/.zerobot_tls"
if [ ! -f "$TLS_DIR/cert.pem" ] || [ ! -f "$TLS_DIR/key.pem" ]; then
  mkdir -p "$TLS_DIR"
  if openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=zerobot" \
       -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" >/dev/null 2>&1; then
    chmod 600 "$TLS_DIR/key.pem"
    echo "[OK] TLS certificate created for the TALK page"
  else
    echo "[WARN] Could not create TLS certificate - TALK page unavailable"
  fi
fi

# ── Power saving (Bluetooth/LEDs off; Pi 3B: USB/Ethernet chip off) ─
# Idempotent: touches nothing when already applied. See powersave.sh.
# Runs the ROOT-OWNED copy installed by install-root-helper.sh, not ./powersave.sh.
asroot powersave install || echo "[WARN] power saving not applied - robot runs, just draws more power"

exit 0
