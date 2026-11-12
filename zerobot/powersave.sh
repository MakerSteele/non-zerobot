#!/bin/bash
# powersave.sh - ZeroBot power-saving settings.
#
#   sudo bash powersave.sh install   apply everything (safe to re-run, e.g. every boot)
#   sudo bash powersave.sh status    show what is active + throttle flags
#   sudo bash powersave.sh remove    undo everything
#
# What it does:
#   1. config.txt block: Bluetooth off (all boards).
#      The Pi's red PWR / green ACT LEDs are deliberately LEFT ON (6 Nov 2026) - they are
#      the quickest way to see boot/power problems, and they only cost a few mA.
#   2. Disables the Bluetooth services.
#   3. Pi 3B and 3B+: boot-time service that cuts power to the USB/Ethernet chip
#      (~200 mA on a 3B+, forum bench measurement). WiFi on both is SDIO, not USB, so
#      it keeps working. USB ports and Ethernet go dead. (11f: 3B+ added - the robot
#      is a 3B+ and the old check skipped it.)
#      To keep USB on (e.g. a keyboard for debugging): create /boot/firmware/zerobot-usb-on
#      from your PC's card reader, or run: sudo systemctl stop zerobot-usb-off
#
# Deliberately NOT done: LEDs off, WiFi power-save (adds FPV latency), undervolting, HDMI tweaks.

set -u

CFG="${ZB_CFG:-/boot/firmware/config.txt}"
[ -f "$CFG" ] || [ -n "${ZB_CFG:-}" ] || CFG=/boot/config.txt
BEGIN="# >>> zerobot-powersave >>>"
END="# <<< zerobot-powersave <<<"
HELPER=/usr/local/sbin/zerobot-usb-off
UNIT=/etc/systemd/system/zerobot-usb-off.service

model() { tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown; }

# Text that goes into config.txt. Starts with [all] so it never inherits a
# section filter from the lines above it. If an older version of this script
# wrote the LED-off lines, re-running install replaces the whole block, so
# they are removed automatically.
cfg_block() {
cat <<EOF
$BEGIN
[all]
dtoverlay=disable-bt
# arm_freq=900   # Pi 3 only, and only if 'vcgencmd get_throttled' still shows undervoltage (then try 700)
$END
EOF
}

# config.txt with our block removed
cfg_stripped() {
    awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$CFG"
}

write_cfg() {   # $1 = "add" or "del"; only touches the file if the result differs
    local tmp; tmp=$(mktemp)
    cfg_stripped > "$tmp"
    if [ "$1" = add ]; then
        # make sure our block starts on a fresh line
        [ -s "$tmp" ] && [ -n "$(tail -c1 "$tmp")" ] && echo >> "$tmp"
        cfg_block >> "$tmp"
    fi
    if cmp -s "$tmp" "$CFG"; then
        echo "[OK]   config.txt already up to date"
        rm -f "$tmp"; return 1
    fi
    [ -f "$CFG.zerobot-bak" ] || cp -p "$CFG" "$CFG.zerobot-bak"
    cat "$tmp" > "$CFG"; rm -f "$tmp"; sync
    echo "[DONE] config.txt updated (backup: $CFG.zerobot-bak) - takes effect after reboot"
    return 0
}

helper_text() {
cat <<'EOF'
#!/bin/sh
# zerobot-usb-off [off|on|status] - power the Pi 3B/3B+ USB/Ethernet chip down (or back up).
P=/sys/devices/platform/soc/3f980000.usb/buspower
case "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)" in
    "Raspberry Pi 3 Model B Rev"*|"Raspberry Pi 3 Model B Plus Rev"*) ;;   # 3B and 3B+ (not 3A+, 4B, Zero)
    *) echo "zerobot-usb-off: not a Pi 3B/3B+, nothing to do"; exit 0 ;;
esac
if [ ! -w "$P" ]; then
    echo "zerobot-usb-off: $P not found, skipping"; exit 0
fi
if [ "${1:-off}" = status ]; then
    # The kernel reads back "Bus Power = 0x0" (or a bare 0): take what follows "= ".
    V=$(cat "$P"); V=${V##*= }; V=$(echo $V)
    case "$V" in 0|0x0) echo "[OK]   USB/Ethernet chip is OFF" ;; *) echo "[INFO] USB/Ethernet chip is ON" ;; esac
    exit 0
fi
if [ "${1:-off}" = on ]; then
    echo 1 > "$P" && echo "zerobot-usb-off: USB/Ethernet chip powered ON"
    exit 0
fi
if [ -e /boot/firmware/zerobot-usb-on ] || [ -e /boot/zerobot-usb-on ]; then
    echo "zerobot-usb-off: zerobot-usb-on file present, leaving USB on"; exit 0
fi
echo 0 > "$P" && echo "zerobot-usb-off: USB/Ethernet chip powered OFF"
exit 0
EOF
}

unit_text() {
cat <<EOF
[Unit]
Description=ZeroBot: power off the Pi 3B/3B+ USB/Ethernet chip
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$HELPER off
ExecStop=$HELPER on

[Install]
WantedBy=multi-user.target
EOF
}

# Installs the helper + unit. Returns 0 if anything changed (daemon-reload needed).
write_helper() {
    local changed=1
    if [ "$(helper_text)" != "$(cat "$HELPER" 2>/dev/null)" ]; then
        helper_text > "$HELPER"; chmod 755 "$HELPER"; changed=0
    fi
    if [ "$(unit_text)" != "$(cat "$UNIT" 2>/dev/null)" ]; then
        unit_text > "$UNIT"; changed=0
    fi
    return $changed
}

bt_services() {   # $1 = disable | enable
    local s
    for s in hciuart.service bluetooth.service; do
        if systemctl cat "$s" >/dev/null 2>&1; then
            if [ "$1" = disable ]; then
                systemctl is-enabled -q "$s" 2>/dev/null && { systemctl disable --now "$s" >/dev/null 2>&1; echo "[DONE] $s disabled"; } \
                    || echo "[OK]   $s already disabled"
            else
                systemctl enable "$s" >/dev/null 2>&1 && echo "[DONE] $s re-enabled"
            fi
        fi
    done
}

do_install() {
    echo "== ZeroBot power saving on: $(model)"
    write_cfg add && REBOOT=1
    bt_services disable
    local usb_changed=0
    if write_helper; then
        systemctl daemon-reload
        usb_changed=1
        echo "[DONE] zerobot-usb-off service installed/updated"
    else
        echo "[OK]   zerobot-usb-off service already installed"
    fi
    systemctl is-enabled -q zerobot-usb-off.service 2>/dev/null || systemctl enable -q zerobot-usb-off.service
    # Already-active oneshot = already ran this boot; starting it again is a no-op.
    # But if the helper just changed (11f: 3B+ added), the run this boot was the OLD
    # one - run the new one now instead of waiting for a reboot.
    if [ "$usb_changed" = 1 ] && systemctl is-active -q zerobot-usb-off.service; then
        "$HELPER" off
    else
        systemctl is-active -q zerobot-usb-off.service || systemctl start zerobot-usb-off.service
    fi
    "$HELPER" status 2>/dev/null
    [ "${REBOOT:-0}" = 1 ] && echo ">> Reboot needed for the config.txt part: sudo reboot"
    return 0
}

do_remove() {
    echo "== Removing ZeroBot power saving"
    systemctl stop zerobot-usb-off.service 2>/dev/null     # ExecStop powers USB back on
    systemctl disable -q zerobot-usb-off.service 2>/dev/null
    rm -f "$UNIT" "$HELPER"; systemctl daemon-reload
    echo "[DONE] USB service removed"
    write_cfg del && echo ">> Reboot to bring Bluetooth back: sudo reboot"
    bt_services enable
    return 0
}

do_status() {
    echo "Board        : $(model)"
    grep -qxF "$BEGIN" "$CFG" && echo "config.txt   : power block present" || echo "config.txt   : power block MISSING"
    grep -q "led_trigger=none" "$CFG" && echo "LEDs         : an led_trigger=none line is still in config.txt (LEDs may be off)" \
        || echo "LEDs         : left on"
    local p=/sys/devices/platform/soc/3f980000.usb/buspower
    if [ -r "$p" ]; then
        # 11h: the kernel reads back "Bus Power = 0x0", not a bare 0 - the old
        # check always said ON.
        local v; v=$(cat "$p"); v=${v##*= }; v=$(echo $v)
        case "$v" in 0|0x0) echo "USB/Ethernet : OFF (saving)" ;; *) echo "USB/Ethernet : ON" ;; esac
    else
        echo "USB/Ethernet : n/a on this board"
    fi
    local h b
    h=$(systemctl is-enabled hciuart 2>/dev/null | head -1); b=$(systemctl is-enabled bluetooth 2>/dev/null | head -1)
    echo "Bluetooth    : hciuart=${h:-none}  bluetooth=${b:-none}"
    if command -v vcgencmd >/dev/null; then
        local t; t=$(vcgencmd get_throttled | cut -d= -f2)
        echo "Throttled    : $t   (0x0 = power is fine)"
        [ "$t" != 0x0 ] && echo "               bit 0 = undervoltage NOW, bit 16 (0x10000) = undervoltage since boot"
    fi
    return 0
}

ACTION="${1:-install}"
case "$ACTION" in
    install|status|remove) ;;
    *) echo "usage: sudo bash $0 [install|status|remove]"; exit 2 ;;
esac
if [ "$ACTION" != status ] && [ "$(id -u)" != 0 ]; then
    echo "run with sudo: sudo bash $0 $ACTION"; exit 1
fi
"do_$ACTION"
