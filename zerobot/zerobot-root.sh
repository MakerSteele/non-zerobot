#!/bin/bash
# zerobot-root - the ONLY things the robot service may do as root.
#
# The service runs as your user with no terminal, so plain `sudo` can't ask for a
# password and silently fails (that is why powersave.sh never ran). Giving
# NOPASSWD to a script in ~/zerobot would let anything that can write there
# become root, so instead:
#   - this file is COPIED to /usr/local/sbin/zerobot-root, owned by root,
#   - powersave.sh is copied to /usr/local/lib/zerobot/, owned by root,
#   - one sudoers rule lets your user run /usr/local/sbin/zerobot-root, nothing else.
# Every job and every package is on a fixed list below; anything else is refused.
#
# Installed/updated by:  sudo bash ~/zerobot/install-root-helper.sh
# setup.sh warns when the copy in ~/zerobot is newer than the installed one.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

POWERSAVE=/usr/local/lib/zerobot/powersave.sh
APT_OK="python3-flask python3-gpiozero python3-picamera2 i2c-tools pigpio python3-pigpio espeak-ng python3-scipy"

refuse() { echo "zerobot-root: refused: $*" >&2; exit 2; }

[ "$(id -u)" = 0 ] || refuse "must be run through sudo"

case "${1:-}" in
  powersave)
    case "${2:-}" in install|status|remove) ;; *) refuse "powersave ${2:-}";; esac
    [ -f "$POWERSAVE" ] || refuse "$POWERSAVE missing - rerun install-root-helper.sh"
    exec /bin/bash "$POWERSAVE" "$2"
    ;;
  apt)
    shift
    [ $# -ge 1 ] || refuse "apt: no package named"
    for p in "$@"; do
      [[ "$p" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || refuse "apt: '$p' is not a plain package name"
      case " $APT_OK " in *" $p "*) ;; *) refuse "apt: $p is not on the list";; esac
    done
    extra=""
    [ "$*" = "python3-picamera2" ] && extra="--no-install-recommends"
    exec apt-get install -y $extra "$@"
    ;;
  i2c)
    command -v raspi-config >/dev/null 2>&1 && raspi-config nonint do_i2c 0
    modprobe i2c-dev 2>/dev/null
    exit 0
    ;;
  pigpiod)
    systemctl enable --now pigpiod 2>/dev/null || pigpiod 2>/dev/null
    exit 0
    ;;
  check)
    echo "zerobot-root: OK"
    exit 0
    ;;
  *)
    refuse "unknown job '${1:-}' (allowed: powersave, apt, i2c, pigpiod, check)"
    ;;
esac
