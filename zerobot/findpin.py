#!/usr/bin/env python3
"""Walk one GPIO at a time, 1s each, and freeze on keypress.

ZeroBot kit debug tool. Whichever pin is lit when you hit a key is the pin
your LED (or meter probe) is on. Press a key again to resume, Ctrl+C to quit
(the held pin is left ON so you can confirm with a meter before walking away).

Found the headlight on 20 Sep 2026 in under a minute, after an evening of
testing the pins the wiring was *supposed* to be on. An output that won't
work is usually attached to a different pin than you think.

LIFT THE WHEELS FIRST - the sweep passes through all four motor lines.
Stop the robot service first too: sudo systemctl stop zerobot
"""
from gpiozero import LED
from time import sleep
import sys, termios, tty, select

# BCM -> physical header pin
PHYS = {2:3, 3:5, 4:7, 5:29, 6:31, 7:26, 8:24, 9:21, 10:19, 11:23,
        12:32, 13:33, 14:8, 15:10, 16:36, 17:11, 18:12, 19:35,
        20:38, 21:40, 22:15, 23:16, 24:18, 25:22, 26:37, 27:13}

# What each pin does in the ZeroBot build (from robot.py, 21 Sep 2026), so the
# answer is immediately useful.
ROLE = {2:"ADS1115 SDA", 3:"ADS1115 SCL", 8:"headlight", 14:"UART TX",
        15:"UART RX", 17:"left motor fwd", 27:"left motor back",
        22:"right motor fwd", 23:"right motor back"}

# Encoders 5/6/13/19 excluded - they drive INTO the Pi, and making them
# outputs would have the Pi and the encoder fighting over the line.
PINS = [2, 3, 4, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18,
        20, 21, 22, 23, 24, 25, 26, 27]

def key_ready():
    return select.select([sys.stdin], [], [], 0)[0] != []

def nap(sec):
    """Sleep in slices so a keypress is caught quickly."""
    steps = int(sec / 0.05)
    for _ in range(steps):
        if key_ready():
            return True
        sleep(0.05)
    return False

leds, failed = {}, []
for p in PINS:
    try:
        leds[p] = LED(p)
    except Exception as e:
        failed.append((p, str(e).split('\n')[0]))

print(f"claimed {len(leds)} pins")
for p, err in failed:
    print(f"  FAILED GPIO {p}: {err}")
print("\nsweeping - press any key to FREEZE on the lit pin\n")

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
held = None
try:
    tty.setcbreak(fd)
    while True:
        for p, d in leds.items():
            role = ROLE.get(p, "")
            label = f"  GPIO {p:2d}   pin {PHYS[p]:2d}   {role}"
            print(label, flush=True)
            d.on()
            hit = nap(1.0)
            if hit:
                sys.stdin.read(1)
                held = p
                print("\n" + "=" * 46)
                print(f"  HELD ON:  GPIO {p}   physical pin {PHYS[p]}")
                if role:
                    print(f"            currently used for: {role}")
                print("=" * 46)
                print("  key = resume    ctrl+C = quit, leaving it ON\n")
                while not key_ready():
                    sleep(0.05)
                sys.stdin.read(1)
                held = None
                print("resuming\n")
            d.off()
            sleep(0.15)
except KeyboardInterrupt:
    print("\nstopped")
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    for p, d in leds.items():
        if p != held:
            d.off()
    print(f"\nleft ON: GPIO {held}" if held else "all pins off")
