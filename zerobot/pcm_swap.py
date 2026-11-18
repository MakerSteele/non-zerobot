#!/usr/bin/env python3
"""pcm_swap.py - lets pigpio's voice and the I2S mic take turns (13k).

Runs as root, only through zerobot-root ("zerobot-root pcm <job>").
Installed to /usr/local/lib/zerobot/ by install-root-helper.sh.

Why: pigpio paces its waves (the robot's voice, the start-up chime) with the
Pi's PCM hardware - the same block the I2S mic uses - and leaves it switched
on afterwards (CS "TXON + DMAEN"). The sound driver then thinks a stream is
already running, skips its own setup, and recordings come back instantly with
garbage. Clearing CS lets the driver set the mic up, but then pigpio's next
wave hangs, because pigpio never sets that hardware up again. So:

    save-clear  before the mic starts: remember pigpio's PCM + PCM-clock
                registers in /run/zerobot-pcm.json, then switch the block off
    restore     before the voice talks / when the mic stops: put pigpio's
                registers back exactly, delete the file
    status      print the registers

Proven by hand 17 Nov 2026 (say -> save-clear -> 6 s recording -> restore ->
say: both phrases played, recording took real time).

13m (17 Nov evening): clearing CS was NOT enough after the first capture of a
boot. The sound driver remembers (regmap cache) what it last wrote and skips
"update" writes that look unchanged. pigpio rewrites DREQ (RX threshold 0 ->
the RX DMA runs free = instant garbage) and the PCM clock divider (10 MHz,
3.25x too fast) behind its back. Proven by hand: DREQ alone -> 0.935 s for
3 s (clock still pigpio's). So save-clear now also
  * copies the driver's cached MODE/RXC/TXC/DREQ/INTEN into the hardware
    (from /sys/kernel/debug/regmap/*i2s*/registers; fallback DREQ 10303e20)
  * stops the PCM clock and loads the driver's own setting (CTL + DIV),
    recorded by "restore" after each capture in KSTATE; fallback = the Pi 3
    48 kHz values seen on 17 Nov (CTL 206, DIV 000a2c2a, PLLD 500 MHz).

save-clear never overwrites an existing save: if a save is already there the
hardware is already in "mic" state, and saving again would store the cleared
registers instead of pigpio's.
"""
import json
import mmap
import os
import struct
import sys
import time

STATE = "/run/zerobot-pcm.json"
KSTATE = "/run/zerobot-pcm-kernel.json"   # 13m: the driver's own clock setting
KCLOCK_FALLBACK = (0x206, 0x000a2c2a)     # Pi 2/3/Zero 2, 48 kHz (17 Nov dump)
KDREQ_FALLBACK = 0x10303e20               # bcm2835-i2s DREQ (17 Nov regmap dump)
PW = 0x5A000000                       # clock-manager password
REGS = {"CS": 0x00, "MODE": 0x08, "RXC": 0x0C, "TXC": 0x10, "DREQ": 0x14,
        "INTEN": 0x18, "GRAY": 0x20}
CM_CTL, CM_DIV = 0x98, 0x9C           # PCM clock in the clock manager


def _bases():
    model = open("/proc/device-tree/model").read()
    if "Pi 4" in model or "Pi 400" in model or "Compute Module 4" in model:
        return 0xFE203000, 0xFE101000
    if "Pi 5" in model:
        raise SystemExit("pcm_swap: Pi 5 not supported (different audio hardware)")
    return 0x3F203000, 0x3F101000     # Pi 2/3/Zero 2 (the original Zero is 0x20..., not used)


def _maps():
    p, c = _bases()
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    return mmap.mmap(fd, 4096, offset=p), mmap.mmap(fd, 4096, offset=c)


def rd(m, o):
    return struct.unpack_from("<I", m, o)[0]


def wr(m, o, v):
    struct.pack_into("<I", m, o, v & 0xFFFFFFFF)


def snapshot(pm, cm):
    s = {k: rd(pm, o) for k, o in REGS.items()}
    s["CMCTL"], s["CMDIV"] = rd(cm, CM_CTL), rd(cm, CM_DIV)
    return s


def _kernel_regs():
    """13m: the sound driver's cached register values {offset: value}, or {}."""
    import glob
    for f in glob.glob("/sys/kernel/debug/regmap/*i2s*/registers"):
        try:
            out = {}
            for line in open(f):
                if ":" in line:
                    a, b = line.split(":", 1)
                    out[int(a, 16)] = int(b.strip(), 16)
            return out
        except (OSError, ValueError):
            pass
    return {}


def _clock_off(cm):
    wr(cm, CM_CTL, PW | (rd(cm, CM_CTL) & 0x2FF & ~0x10))   # ENAB off, same source
    t = time.time()
    while rd(cm, CM_CTL) & 0x80 and time.time() - t < 0.05:
        pass                                                # wait until not busy


def _kernel_side(pm, cm):
    """13m: make the hardware match what the driver THINKS it holds."""
    k = _kernel_regs()
    for o in (0x08, 0x0C, 0x10, 0x18):                      # MODE RXC TXC INTEN
        if o in k:
            wr(pm, o, k[o])
    wr(pm, REGS["DREQ"], k.get(0x14, KDREQ_FALLBACK))
    ctl, div = KCLOCK_FALLBACK
    try:
        with open(KSTATE) as f:
            j = json.load(f)
        ctl, div = j["CMCTL"], j["CMDIV"]
    except (OSError, ValueError, KeyError):
        pass
    _clock_off(cm)
    wr(cm, CM_DIV, PW | (div & 0xFFFFFF))
    wr(cm, CM_CTL, PW | (ctl & 0x2FF & ~0x10))              # driver's source, still off


def main(job):
    pm, cm = _maps()
    if job == "status":
        if os.path.exists(KSTATE):
            print("driver clock:", open(KSTATE).read().strip())
        print(" ".join(f"{k} {v:08x}" for k, v in snapshot(pm, cm).items()),
              "| saved" if os.path.exists(STATE) else "| nothing saved")
        return 0
    if job == "save-clear":
        if not os.path.exists(STATE):
            s = snapshot(pm, cm)
            tmp = STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(s, f)
            os.replace(tmp, STATE)
        wr(pm, REGS["CS"], 0)         # mic side may now set the block up
        _kernel_side(pm, cm)          # 13m
        print(f"cleared, CS {rd(pm, 0):08x}  DREQ {rd(pm, 0x14):08x}  "
              f"CMCTL {rd(cm, CM_CTL):08x} CMDIV {rd(cm, CM_DIV):08x}")
        return 0
    if job == "restore":
        if not os.path.exists(STATE):
            print("nothing saved")
            return 0
        with open(STATE) as f:
            s = json.load(f)
        # 13m: remember the driver's clock setting (it switched the clock off
        # when the capture closed, so ENAB is clear and DIV is its own)
        kc, kd = rd(cm, CM_CTL), rd(cm, CM_DIV)
        if not kc & 0x10 and kc & 0xF and kd != s["CMDIV"]:
            tmp = KSTATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"CMCTL": kc & 0x2FF, "CMDIV": kd & 0xFFFFFF}, f)
            os.replace(tmp, KSTATE)
        wr(pm, REGS["CS"], 0)                                   # stop the mic side first
        wr(cm, CM_CTL, PW | (s["CMCTL"] & 0x2FF & ~0x10))      # clock off, same source
        t = time.time()
        while rd(cm, CM_CTL) & 0x80 and time.time() - t < 0.05:
            pass                                                # wait until not busy
        wr(cm, CM_DIV, PW | (s["CMDIV"] & 0xFFFFFF))
        wr(cm, CM_CTL, PW | (s["CMCTL"] & 0x2FF))              # clock back as pigpio had it
        for k in ("MODE", "RXC", "TXC", "DREQ", "INTEN", "GRAY"):
            wr(pm, REGS[k], s[k])
        wr(pm, REGS["CS"], s["CS"])                             # last: pigpio's on-switches
        os.remove(STATE)
        print(f"restored, CS {rd(pm, 0):08x}")
        return 0
    print("usage: pcm_swap.py save-clear|restore|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
