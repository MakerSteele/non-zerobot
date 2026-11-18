#!/bin/bash
# install-root-helper.sh - ONE-TIME (and after powersave.sh or zerobot-root.sh change):
#
#     sudo bash ~/zerobot/install-root-helper.sh
#
# Copies zerobot-root.sh and powersave.sh to root-owned places and adds one
# sudoers rule so the robot service can run them without a password.
# Safe to re-run. Undo:  sudo rm /etc/sudoers.d/zerobot-root /usr/local/sbin/zerobot-root
#                        sudo rm -r /usr/local/lib/zerobot
set -u
cd "$(dirname "$0")" || exit 1

if [ "$(id -u)" != 0 ]; then
  echo "run with sudo:  sudo bash $0"; exit 1
fi
U="${SUDO_USER:-}"
if [ -z "$U" ] || [ "$U" = root ]; then
  echo "run it from your own login with sudo (not as root directly), so the rule is for the right user"; exit 1
fi
for f in zerobot-root.sh powersave.sh pcm_swap.py; do
  [ -f "$f" ] || { echo "[FAIL] $f not found next to this script"; exit 1; }
done

install -o root -g root -m 755 zerobot-root.sh /usr/local/sbin/zerobot-root
install -d -o root -g root -m 755 /usr/local/lib/zerobot
install -o root -g root -m 644 powersave.sh /usr/local/lib/zerobot/powersave.sh
install -o root -g root -m 644 pcm_swap.py /usr/local/lib/zerobot/pcm_swap.py
echo "[DONE] helper installed: /usr/local/sbin/zerobot-root (+ powersave.sh, pcm_swap.py in /usr/local/lib/zerobot)"

RULE="/etc/sudoers.d/zerobot-root"
TMP=$(mktemp)
echo "$U ALL=(root) NOPASSWD: /usr/local/sbin/zerobot-root" > "$TMP"
# Never install a sudoers file that doesn't parse - a broken one locks sudo out.
if visudo -cqf "$TMP"; then
  install -o root -g root -m 440 "$TMP" "$RULE"
  echo "[DONE] sudoers rule: $(cat "$RULE")"
else
  echo "[FAIL] sudoers rule did not validate - nothing changed"; rm -f "$TMP"; exit 1
fi
rm -f "$TMP"

if sudo -u "$U" sudo -n /usr/local/sbin/zerobot-root check >/dev/null 2>&1; then
  echo "[OK]   $U can run the helper without a password"
else
  echo "[FAIL] $U still can't run the helper without a password - copy the output above into an issue at github.com/MakerSteele/non-zerobot/issues"; exit 1
fi

echo "== Applying power saving now"
/usr/local/sbin/zerobot-root powersave install
