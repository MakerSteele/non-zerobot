#!/bin/bash
# deploy.sh - install ~/zerobot.zip and PROVE the new build is running.
#
# From the PC, the one-line deploy runs this straight out of the new zip, so
# the newest deploy.sh is always the one used. On the Pi:  bash ~/zerobot/deploy.sh
#
# Steps: check zip -> pause watchdog + stop service -> replace ~/zerobot ->
# sync (11 Nov: a power cut after deploy left every file 0 bytes) -> check no
# file came out empty -> start -> wait for /version to report the new build.
set -u
ZIP="${1:-$HOME/zerobot.zip}"
DIR="$HOME/zerobot"

fail() { echo "[FAIL] $*"; exit 1; }

[ -f "$ZIP" ] || fail "$ZIP not found - copy it from the PC first"
unzip -tq "$ZIP" >/dev/null 2>&1 || fail "the zip is damaged - copy it again"
NEW=$(unzip -p "$ZIP" version.py 2>/dev/null | sed -n 's/^BUILD *= *"\(.*\)".*/\1/p')
[ -n "$NEW" ] || fail "no version.py in the zip - is it the right file?"
echo "== Deploying build $NEW"
T0=$(date +%s)

# The watchdog restarts a service that stops answering - pause it so it can't
# start the robot halfway through the unzip.
sudo systemctl stop zerobot-watchdog.timer zerobot || fail "couldn't stop the service"

rm -rf "$DIR" && unzip -q "$ZIP" -d "$DIR" || fail "unzip failed"
sync
echo "[OK]   files written and synced to the SD card"

bad=0
while read -r size name; do
  if [ "$size" -gt 0 ] && [ ! -s "$DIR/$name" ]; then
    echo "[FAIL] $name came out empty"; bad=1
  fi
done < <(unzip -l "$ZIP" | awk '$1 ~ /^[0-9]+$/ && NF >= 4 {print $1, $4}')
[ "$bad" = 0 ] || fail "some files are empty - run deploy again"

sudo systemctl reset-failed zerobot 2>/dev/null
sudo systemctl start zerobot zerobot-watchdog.timer || fail "couldn't start the service"

echo -n "       waiting for the robot to answer"
got=""
for _ in $(seq 1 90); do
  got=$(curl -fs --max-time 2 http://127.0.0.1:5000/version 2>/dev/null \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["build"])' 2>/dev/null)
  [ -n "$got" ] && break
  echo -n "."; sleep 1
done
echo

if [ "$got" = "$NEW" ]; then
  echo "[OK]   running build $got - reopen the page on the phone"
  journalctl -u zerobot --since "@$T0" --no-pager -o cat 2>/dev/null | grep -E "^\[WARN\]" | tail -5
  exit 0
fi
if [ -n "$got" ]; then
  echo "[FAIL] robot answers but reports build $got, expected $NEW"
else
  echo "[FAIL] robot didn't answer within 90 s"
fi
echo "------ last 30 log lines ------"
journalctl -u zerobot -n 30 --no-pager
exit 1
