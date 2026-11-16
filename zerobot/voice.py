#!/usr/bin/env python3
"""
voice.py - the robot talks through its drive motors.

The motor coils act as crude speakers. Audio becomes ~16 kHz PWM on the TB6612
inputs, timed by pigpio's DMA waveforms so Python jitter never reaches the
sound. Positive audio = "forward" pulses, negative = "backward", silence = all
pins off, so the average push is near zero and the robot only twitches.

Proven by motor_speak.py on 10 Nov 2026 ("take me to your leader" was clear at
level 6). This module is the same method, made to stream:

  * play_clip(samples)  - a recording or uploaded file (already 16 kHz PCM)
  * say(text)           - espeak-ng text-to-speech
  * live_feed(block)    - live talk from the phone, a block at a time

Only one sound plays at a time. While `busy` is True the motor pins belong to
this module and robot.py must not drive (it checks voice.busy in /move).

Every audio path runs through Shaper, which does: bass cut -> automatic level
(AGC) -> noise gate -> consonant lift -> overdrive -> scale to motor duty.
"""
import os
import struct
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np
import pigpio

# scipy runs the two filters in C. Without it they run as Python loops, which
# work but cost several times more CPU on a Pi 3 (setup.sh installs it).
try:
    from scipy.signal import lfilter as _lfilter
except Exception:
    _lfilter = None

# ── Timing ───────────────────────────────────────────────────────────────────
# pigpio waves have 1 us resolution, and 1e6 / 16000 = 62.5 us. Alternating
# 62 and 63 us cycles averages exactly 62.5, so the motors play at exactly
# 16 kHz and live audio from the phone (also 16 kHz) never drifts out of step.
RATE = 16000
CHUNK = 500                  # samples per DMA wave (31 ms). Bigger ran pigpio out of wave memory.

# ── Sound shaping ────────────────────────────────────────────────────────────
HIGHPASS_HZ = 250            # motors can't play bass; it only shakes the robot
# CLARITY 0-10 (11h): each step raises the bass cut 30 Hz (250 -> 550 at 10) and
# adds 0.6 dB of treble above TREBLE_HZ (+6 dB at 10). For INTELLIGIBILITY:
# the lows add the least to understanding words. It barely changes current
# (simulated: ~4% less raw drive at 10) because the AGC and overdrive bring the
# level back up - POWER is what bounds current. CLARITY 0 is exactly the 11g sound.
CLARITY_HP_STEP = 30.0
CLARITY_DB_STEP = 0.6
TREBLE_HZ = 2000.0
PEAK_FLOOR = 0.02            # AGC never boosts more than 1/PEAK_FLOOR (stops hiss being blown up)
AGC_RELEASE = 0.985          # per chunk: loud-then-quiet recovers over ~2 s
# Noise gate. v1 (10 Nov) muted any 6 ms block under 0.10 and chopped the
# quiet parts of words out - "like a bad cell connection". Now: lower
# threshold, and once open it HOLDS open for 150 ms, so it only closes in
# real pauses between words.
GATE = 0.04                  # opens when a block is louder than this fraction of the recent peak...
GATE_ABS = 0.006             # ...AND louder than this in absolute terms (about -44 dBFS), so
                             # room hiss can't open it even after AGC would have boosted it
GATE_BLOCK = 100             # samples per gate block (6.25 ms)
GATE_HOLD = 24               # blocks the gate stays open after the last loud one (150 ms)
GATE_LOOKAHEAD = 2           # blocks the gate opens EARLY, before a loud one (12 ms)

# ── Live talk ────────────────────────────────────────────────────────────────
LIVE_PREBUFFER = 8 * CHUNK   # wait for 250 ms of audio before playing (v1: 94 ms was too thin for WiFi)
LIVE_REBUFFER = 5 * CHUNK    # after running dry mid-sentence, wait for 156 ms before resuming
LIVE_MAX_BUFFER = RATE       # never let the backlog exceed 1 s - drop oldest instead
LIVE_TIMEOUT = 1.0           # no audio from the phone for this long = talk session over

CLIP_MAX_SECONDS = 60

# ── Power limit ──────────────────────────────────────────────────────────────
# Talking motors never turn, so they draw close to STALL current: no back-EMF.
# At loudness 6 the overdrive held both motors near full power for as long as
# you spoke, the pack sagged, and the Pi browned out and rebooted (10 Nov 2026:
# uptime 1 min, get_throttled 0xd0000 = under-voltage had occurred).
# The limiter keeps the AVERAGE drive (over ~0.1 s) at or below the budget.
# Short peaks still get through, so speech stays clear; long loud stretches
# are turned down.
POWER_DEFAULT = 0.75         # fraction of full motor power, averaged. 75% picked by ear
                             # (11 Nov). Not yet proven on a 30 s talk - drop it if the Pi browns out.
LIM_ATTACK = 0.7             # per 6 ms block: reacts within ~10 ms
LIM_RELEASE = 0.05           # per 6 ms block: lets go over ~0.1 s


def level_settings(level, smooth=5, clarity=0):
    """Map loudness 1-10, smoothness 0-10 and clarity 0-10 to
    (max duty, drive, high-pass Hz, low-pass Hz, treble gain).
    Treble gain is the EXTRA amount of the >2 kHz band added back in
    (0 = flat, 1.0 = +6 dB).
    Loudness 1 = the first motor_speak test (duty 0.6, no overdrive). Duty maxes
    out by level 4; above that, loudness comes from overdrive. 7-10 let more
    bass through = more twitching.
    Smooth 0 = the 10b sound (no low-pass, full overdrive). Higher = less
    overdrive and a lower low-pass: rounder, slightly quieter, less crisp."""
    level = min(max(float(level), 1.0), 10.0)
    smooth = min(max(float(smooth), 0.0), 10.0)
    clarity = min(max(float(clarity), 0.0), 10.0)
    duty = 0.6 + 0.35 * min(1.0, (level - 1) / 3.0)          # 0.60 -> 0.95
    drive = 1.0 + (level - 1) * 0.8                            # 1.0  -> 8.2
    drive = 1.0 + (drive - 1.0) * (1.0 - 0.06 * smooth)        # smooth 10 keeps 40% of the overdrive
    hp = HIGHPASS_HZ if level <= 6 else HIGHPASS_HZ - (level - 6) * 30   # 250 -> 130
    hp += clarity * CLARITY_HP_STEP                            # +0 -> +300 Hz
    lp = None if smooth <= 0 else 7000.0 - 450.0 * smooth      # 6550 Hz -> 2500 Hz
    treble = 10.0 ** (clarity * CLARITY_DB_STEP / 20.0) - 1.0  # 0 -> 1.0 (+6 dB)
    return duty, drive, hp, lp, treble


class Shaper:
    """Turns audio (floats, -1..1, 16 kHz) into motor duty, chunk by chunk.
    Keeps its filter, AGC, gate and dither state between chunks so streaming
    has no seams."""

    def __init__(self, level, init_peak=PEAK_FLOOR, smooth=5, power=POWER_DEFAULT, clarity=0):
        self.b = 0.0
        # treble shelf = signal + treble * (first-order high-pass at TREBLE_HZ).
        # Its corner never changes, so its state never needs rescaling.
        self.ta = 1.0 / (1.0 + 2.0 * np.pi * TREBLE_HZ / RATE)
        self.tx = 0.0
        self.ty = 0.0
        self.t_zi = np.zeros(1)
        self.treble = 0.0
        self._pending = None        # (level, smooth, power) waiting for the next chunk
        self.px = 0.0
        self.py = 0.0
        self.lp = 0.0
        self.peak = max(init_peak, PEAK_FLOOR)
        self.hold = 0
        self.gain = 0.0             # gate gain, ramps 0 <-> 1 so it fades, never clicks
        self.dither = [0.0]         # rounding error carried between pulses (see build_pulses)
        self.hp_zi = np.zeros(1)    # filter states for the scipy path
        self.lp_zi = np.zeros(1)
        self.env = 0.0              # limiter's running average of |drive|
        self.lim_gain = 1.0
        self.avg_drive = 0.0        # average |drive| of the last chunk, for the status readout
        self._apply(level, smooth, power, clarity)

    def _apply(self, level, smooth, power, clarity=0):
        """Set the coefficients. Filter, AGC, gate, limiter and dither STATE is
        left alone, so a change mid-sentence has no click: the limiter glides
        to the new budget over a block or two."""
        self.duty, self.drive, hp, lp, self.treble = level_settings(level, smooth, clarity)
        a = 1.0 / (1.0 + 2.0 * np.pi * hp / RATE)
        # scipy's saved filter state is scaled by the coefficient
        # (high-pass: a*(y-x), low-pass: (1-b)*y), so rescale it when the
        # coefficient changes - otherwise one sample glitches.
        if getattr(self, "a", None):
            self.hp_zi = self.hp_zi * (a / self.a)
        self.a = a
        b = 0.0 if lp is None else 1.0 - np.exp(-2.0 * np.pi * lp / RATE)
        if b and not self.b:        # low-pass switching back on: its state is stale
            self.lp = 0.0
            self.lp_zi = np.zeros(1)
        elif b and self.b:
            self.lp_zi = self.lp_zi * ((1.0 - b) / (1.0 - self.b))
        self.b = b
        self.budget = min(max(float(power), 0.05), 1.0)

    def retune(self, level, smooth, power, clarity=0):
        """Safe to call from another thread while audio plays. The new settings
        are picked up at the start of the next chunk (within ~31 ms), never
        halfway through one."""
        self._pending = (level, smooth, power, clarity)

    def process(self, x):
        pend = self._pending
        if pend is not None:
            self._pending = None
            self._apply(*pend)
        # first-order high-pass: y[n] = a * (y[n-1] + x[n] - x[n-1])
        a = self.a
        if _lfilter is not None:
            y, self.hp_zi = _lfilter([a, -a], [1.0, -a], np.asarray(x, dtype=np.float64), zi=self.hp_zi)
        else:
            px = self.px
            py = self.py
            y = np.empty(len(x))
            for i, v in enumerate(x):
                py = a * (py + v - px)
                px = v
                y[i] = py
            self.px = px
            self.py = py

        # treble shelf (CLARITY). The filter always runs so its state is
        # current when CLARITY is raised mid-sound; at CLARITY 0 it adds 0.
        ta = self.ta
        if _lfilter is not None:
            t, self.t_zi = _lfilter([ta, -ta], [1.0, -ta], y, zi=self.t_zi)
        else:
            tx = self.tx
            ty = self.ty
            t = np.empty(len(y))
            for i, v in enumerate(y):
                ty = ta * (ty + v - tx)
                tx = v
                t[i] = ty
            self.tx = tx
            self.ty = ty
        if self.treble:
            y = y + self.treble * t

        # AGC: instant attack, slow release
        blk_peak = float(np.max(np.abs(y))) if len(y) else 0.0
        if blk_peak > self.peak:
            self.peak = blk_peak
        else:
            self.peak = max(self.peak * AGC_RELEASE, PEAK_FLOOR)

        # noise gate with hold - judged on the real level, BEFORE the AGC boost.
        # Opening and closing fade across one 6 ms block instead of switching.
        # LOOKAHEAD: a block also opens the gate if one of the next
        # GATE_LOOKAHEAD blocks in this chunk is loud, so soft word starts
        # ("h", "s", "f") aren't clipped waiting for the vowel.
        threshold = max(GATE_ABS, GATE * self.peak)
        starts = list(range(0, len(y), GATE_BLOCK))
        loud = [np.sqrt(np.mean(y[s:s + GATE_BLOCK] ** 2)) >= threshold for s in starts]
        for k, s in enumerate(starts):
            seg = y[s:s + GATE_BLOCK]
            if any(loud[k:k + 1 + GATE_LOOKAHEAD]):
                self.hold = GATE_HOLD
                target = 1.0
            elif self.hold > 0:
                self.hold -= 1
                target = 1.0
            else:
                target = 0.0
            if target != self.gain:
                y[s:s + GATE_BLOCK] = seg * np.linspace(self.gain, target, len(seg))
            elif target == 0.0:
                y[s:s + GATE_BLOCK] = 0.0
            self.gain = target

        y = np.clip(y / self.peak, -1.0, 1.0)
        y = np.sign(y) * np.sqrt(np.abs(y))                   # lift consonants
        y = np.tanh(self.drive * y) / np.tanh(self.drive)     # overdrive

        # low-pass: takes the fizz off the overdrive
        # y[n] = y[n-1] + b * (x[n] - y[n-1])
        if self.b:
            b = self.b
            if _lfilter is not None:
                y, self.lp_zi = _lfilter([b], [1.0, -(1.0 - b)], y, zi=self.lp_zi)
                if self.gain == 0.0 and abs(self.lp_zi[0]) < 1e-3:
                    self.lp_zi[0] = 0.0
            else:
                lp = self.lp
                for i, v in enumerate(y):
                    lp += b * (v - lp)
                    y[i] = lp
                self.lp = lp
                if self.gain == 0.0 and abs(lp) < 1e-3:
                    self.lp = 0.0
        d = y * self.duty

        # power limit: feed-forward, so the output average lands on the budget
        if self.budget < 1.0:
            for s in range(0, len(d), GATE_BLOCK):
                seg = d[s:s + GATE_BLOCK]
                m = float(np.mean(np.abs(seg)))
                self.env += (LIM_ATTACK if m > self.env else LIM_RELEASE) * (m - self.env)
                g = 1.0 if self.env <= self.budget else self.budget / self.env
                if g != self.lim_gain:
                    d[s:s + GATE_BLOCK] = seg * np.linspace(self.lim_gain, g, len(seg))
                elif g != 1.0:
                    d[s:s + GATE_BLOCK] = seg * g
                self.lim_gain = g
        self.avg_drive = float(np.mean(np.abs(d))) if len(d) else 0.0
        return d


def build_pulses(duty, fwd_mask, back_mask, dither=None):
    """One PWM cycle per sample, alternating 62/63 us. Every cycle ends with
    all pins off, so an "on" pulse needs no off-mask (masks cost wave memory).
    Runs of silence merge into one long pulse.

    dither: a one-item list carrying the rounding error between calls. Each
    pulse can only be a whole number of microseconds (~60 steps), which makes
    quiet sound gritty. Carrying the leftover fraction into the next pulse
    (error diffusion) pushes that grit up to frequencies the motor barely
    plays. Exact zeros (gate closed) reset it, so silence stays silent."""
    all_mask = fwd_mask | back_mask
    pulses = []
    err = dither[0] if dither is not None else 0.0
    for i, d in enumerate(duty):
        period = 62 + (i & 1)
        if d == 0.0:
            err = 0.0
            t = 0
        else:
            v = d * period + err
            t = int(round(v))
            if t >= period:
                t = period - 1
            elif t <= -period:
                t = -(period - 1)
            err = v - t
            if err > 1.0:
                err = 1.0
            elif err < -1.0:
                err = -1.0
        if t == 0:
            if pulses and pulses[-1].gpio_on == 0:
                pulses[-1].delay += period
            else:
                pulses.append(pigpio.pulse(0, all_mask, period))
        else:
            on = fwd_mask if t > 0 else back_mask
            t = abs(t)
            pulses.append(pigpio.pulse(on, 0, t))
            pulses.append(pigpio.pulse(0, on, period - t))
    if dither is not None:
        dither[0] = err
    return pulses


TAIL_US = 20                 # 11k: every chunk ends on a 20 us pure-delay pulse (see build_rows)
# 11l: every wave is created at the SAME padded size (wave_create_and_pad).
# pigpio allocates wave memory like a stack: deleting a wave only frees it if
# it's the newest one, otherwise its space is only reused by a later wave of
# EXACTLY the same size. While streaming there is always a newer wave alive,
# so differently sized chunks leaked until "No more CBs for waveform"
# (11 Nov, LOUDNESS 3 / SMOOTH 7: sizes vary a lot). A worst-case chunk needs
# ~2,004 of the 25,016 control blocks (8%); 30% leaves room for 3 at once.
WAVE_PAD_PCT = 30


def _diffuse(duty, dither=None, fixed62=False, use_dither=True):
    """Vectorised version of the error diffusion in build_pulses (11i).

    build_pulses rounds each pulse to whole microseconds and carries the
    leftover into the next one: err = v - round(v), v = d*period + err.
    The running total of the rounded pulses is then always round(running
    total of the exact pulses) - so t = diff(round(cumsum)) gives the same
    pulses in one numpy pass instead of a Python loop. An exact zero (gate
    closed) resets the carry, as before. The clips in build_pulses can't
    trigger while |duty| <= 0.95 (60.4 < 62), which Shaper guarantees.
    Returns (t, period) as int arrays; t is signed microseconds.

    11m test switches (talk page, "TONE TEST"):
      fixed62    - every cycle 62 us instead of alternating 62/63. The 62/63
                   pattern repeats every 2 samples = an 8 kHz tone under the
                   voice (suspect for "tinny"). 62 us plays 0.8% fast (16.13
                   kHz) - a pitch change of 1/7 of a semitone, inaudible.
      use_dither - False = plain rounding, no error carried between pulses.
                   Tests whether the dither hiss is the tinny sound (expect
                   quiet parts to sound grittier with it off)."""
    d = np.asarray(duty, dtype=np.float64)
    n = len(d)
    per = np.full(n, 62, dtype=np.int64) if fixed62 else 62 + (np.arange(n) & 1)
    if n == 0:
        return np.zeros(0, dtype=np.int64), per
    if not use_dither:
        t = np.round(d * per).astype(np.int64)
        t[d == 0.0] = 0
        np.clip(t, -(per - 1), per - 1, out=t)
        if dither is not None:
            dither[0] = 0.0
        return t, per
    e0 = dither[0] if dither is not None else 0.0
    z = d == 0.0
    S = np.cumsum(d * per)
    last_zero = np.where(z, np.arange(n), -1)
    np.maximum.accumulate(last_zero, out=last_zero)
    base = np.where(last_zero >= 0, S[np.maximum(last_zero, 0)], -e0)
    R = S - base                       # exact running total since the last reset
    T = np.round(R)                    # rounded running total
    t = np.diff(T, prepend=0.0).astype(np.int64)
    t[z] = 0
    np.clip(t, -(per - 1), per - 1, out=t)      # safety only, never hit
    if dither is not None:
        dither[0] = 0.0 if z[-1] else float(R[-1] - T[-1])
    return t, per


def build_rows(duty, fwd_mask, back_mask, dither=None, fixed62=False, use_dither=True):
    """The same pulses as build_pulses (11i), as an (N, 3) uint32 array of
    (gpio_on, gpio_off, delay) - ready to hand to pigpio as bytes.
    Identical pulse for pulse, including how silence is merged:
    a silent run at the start of the chunk is one all-off pulse; a silent run
    after a sound sample just lengthens that sample's off pulse."""
    t, per = _diffuse(duty, dither, fixed62, use_dither)
    n = len(t)
    if n == 0:
        return np.zeros((0, 3), dtype=np.uint32)
    all_mask = fwd_mask | back_mask
    nz = t != 0
    zero = ~nz
    at = np.abs(t)
    on = np.where(t > 0, fwd_mask, back_mask)
    off_delay = per - at
    # silent runs: [start, end] index pairs and their total length
    starts = np.flatnonzero(zero & np.concatenate(([True], nz[:-1])))
    ends = np.flatnonzero(zero & np.concatenate((nz[1:], [True])))
    cz = np.cumsum(np.where(zero, per, 0))
    run_len = cz[ends] - cz[starts] + per[starts]
    lead = None
    if len(starts) and starts[0] == 0:
        lead = int(run_len[0])
        starts, run_len = starts[1:], run_len[1:]
    off_delay[starts - 1] += run_len               # fold silence into the off pulse before it
    k = np.flatnonzero(nz)
    rows = np.zeros((len(k), 2, 3), dtype=np.uint32)
    rows[:, 0, 0] = on[k]
    rows[:, 0, 2] = at[k]
    rows[:, 1, 1] = on[k]
    rows[:, 1, 2] = off_delay[k]
    rows = rows.reshape(-1, 3)
    if lead is not None:
        rows = np.concatenate((np.array([[0, all_mask, lead]], dtype=np.uint32), rows))
    # 11k: end every chunk on a SHORT delay. pigpio turns each delay into one
    # DMA control block, and links the next wave (SYNC) by writing into the
    # LAST block of the playing wave. The DMA engine reads a block's "next"
    # address when it LOADS that block, so once it has loaded the last block
    # the link is never seen and the next wave never starts. A chunk ending in
    # silence ended on one long block (a fully silent chunk = one 31 ms block),
    # so ~1 in 5 links was lost (11 Nov: 217 relinks in a 33 s clip). A pulse
    # that changes no pins is a pure delay, so splitting the last delay leaves
    # the timing on the motor pins exactly as it was.
    if len(rows) and rows[-1, 2] > 2 * TAIL_US:
        rows[-1, 2] -= TAIL_US
        rows = np.concatenate((rows, np.array([[0, 0, TAIL_US]], dtype=np.uint32)))
    return rows


def resample(x, src_rate):
    """Linear resample to RATE. Fine for speech through a motor."""
    if src_rate == RATE or len(x) < 2:
        return x
    t_old = np.arange(len(x)) / float(src_rate)
    t_new = np.arange(0.0, t_old[-1], 1.0 / RATE)
    return np.interp(t_new, t_old, x)


def pcm16_to_float(raw):
    """Little-endian signed 16-bit bytes -> floats -1..1."""
    if len(raw) % 2:
        raw = raw[:-1]
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


class Voice:
    def __init__(self, pi, pin_pairs, stop_motors=None):
        """pi: connected pigpio.pi. pin_pairs: [(fwd, back), ...] for each
        motor. stop_motors: called before speaking so gpiozero lets go."""
        self.pi = pi
        self.pins = [g for pair in pin_pairs for g in pair]
        self.fwd_mask = sum(1 << f for f, _ in pin_pairs)
        self.back_mask = sum(1 << b for _, b in pin_pairs)
        self.stop_motors = stop_motors
        self.level = 6.0
        self.smooth = 5.0
        self.power = POWER_DEFAULT
        self.clarity = 0.0
        self.avg_drive = 0.0
        self._shaper = None         # the Shaper of whatever is playing now
        self.busy = False
        self.mode = None            # "clip", "say", "live"
        self.last_error = None
        self.underruns = 0          # pigpio ran out of audio: Pi too slow to keep up
        self.dropouts = 0           # live audio from the phone arrived late: WiFi
        self.late_blocks = 0        # phone blocks that arrived out of order and were dropped
        self.buffer_ms = 0
        self.work_ms = 0.0          # 11i: average Pi time to prepare one 31 ms chunk
        self.work_ms_max = 0.0
        self.relinks = 0            # 11j: lost SYNC links recovered in the last playback
        # 11m tone-test switches (see _diffuse). Read at every chunk, so they
        # change the sound that is playing now. Not saved: a restart = defaults.
        self.fixed62 = False
        self.use_dither = True
        # 11i: hand pigpio the pulses as one block of bytes instead of 1000
        # Python objects. Uses pigpio's own internal call (the one
        # wave_add_generic makes); if this pigpio doesn't have it, the normal
        # wave_add_generic path is used.
        self._fast = (all(hasattr(pigpio, n) for n in ("_pigpio_command_ext", "_PI_CMD_WVAG", "_u2i"))
                      and hasattr(pi, "sl"))
        self._fast_checked = False
        self._pad = hasattr(pi, "wave_create_and_pad")   # 11l, see WAVE_PAD_PCT
        self._lock = threading.Lock()
        self._abort = False
        # live talk state
        self._live_lock = threading.Lock()
        self._live_buf = np.zeros(0)
        self._live_last = 0.0
        self._live_ended = False
        self._live_seq = -1
        self._live_sid = None
        self._live_active = False   # True while a live session is still accepting audio
        self._live_old_sids = []
        self._live_feed_lock = threading.Lock()

    # ── public ───────────────────────────────────────────────────────────────
    def status(self):
        return {"available": True, "busy": self.busy, "mode": self.mode,
                "level": self.level, "smooth": self.smooth, "power": self.power,
                "clarity": self.clarity,
                "drive": round(self.avg_drive, 2) if self.busy else 0.0,
                "underruns": self.underruns,
                "dropouts": self.dropouts, "late_blocks": self.late_blocks,
                "buffer_ms": self.buffer_ms, "error": self.last_error,
                "work_ms": round(self.work_ms, 1), "work_ms_max": round(self.work_ms_max, 1),
                "fast_wave": self._fast, "relinks": self.relinks,
                "fixed62": self.fixed62, "dither": self.use_dither}

    def set_level(self, level):
        self.level = min(max(float(level), 1.0), 10.0)
        self._retune()
        return self.level

    def set_power(self, power):
        self.power = min(max(float(power), 0.05), 1.0)
        self._retune()
        return self.power

    def set_smooth(self, smooth):
        self.smooth = min(max(float(smooth), 0.0), 10.0)
        self._retune()
        return self.smooth

    def set_clarity(self, clarity):
        self.clarity = min(max(float(clarity), 0.0), 10.0)
        self._retune()
        return self.clarity

    def set_tone_test(self, fixed62=None, dither=None):
        """11m: A/B switches for the tinny tone. Take effect on the next chunk."""
        if fixed62 is not None:
            self.fixed62 = bool(fixed62)
        if dither is not None:
            self.use_dither = bool(dither)

    def _retune(self):
        """11c: slider changes reach the sound that is playing NOW, not just
        the next one."""
        sh = self._shaper
        if sh is not None and self.busy:
            sh.retune(self.level, self.smooth, self.power, self.clarity)

    def stop(self):
        """Cut whatever is playing. Returns at once; the worker cleans up."""
        self._abort = True
        with self._live_lock:
            self._live_ended = True

    def play_clip(self, samples, src_rate=RATE):
        """samples: float array -1..1. Returns (ok, message)."""
        x = resample(np.asarray(samples, dtype=np.float64), src_rate)
        if len(x) == 0:
            return False, "empty clip"
        x = x[:int(CLIP_MAX_SECONDS * RATE)]
        shaper = Shaper(self.level, init_peak=float(np.max(np.abs(x))) * 0.5,
                        smooth=self.smooth, power=self.power, clarity=self.clarity)
        pos = [0]
        self._shaper = shaper

        def source():
            i = pos[0]
            if i >= len(x):
                return None
            pos[0] = i + CHUNK
            seg = x[i:i + CHUNK]
            if len(seg) < CHUNK:
                seg = np.concatenate([seg, np.zeros(CHUNK - len(seg))])
            out = shaper.process(seg)
            self.avg_drive = shaper.avg_drive
            return out

        return self._start(source, "clip", f"{len(x) / RATE:.1f} s")

    def say(self, text):
        text = (text or "").strip()[:300]
        if not text:
            return False, "nothing to say"
        path = os.path.join(tempfile.gettempdir(), "zerobot_say.wav")
        try:
            subprocess.run(["espeak-ng", "-s", "130", "-w", path, text],
                           check=True, timeout=15, capture_output=True)
        except FileNotFoundError:
            return False, "espeak-ng is not installed (sudo apt install espeak-ng)"
        except Exception as e:
            return False, f"espeak-ng failed: {e}"
        with wave.open(path, "rb") as w:
            ch = w.getnchannels()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        x = pcm16_to_float(raw)
        if ch > 1:
            x = x[:len(x) - len(x) % ch].reshape(-1, ch).mean(axis=1)
        ok, msg = self.play_clip(x, rate)
        if ok:
            self.mode = "say"
        return ok, msg

    def live_feed(self, raw, seq=None, end=False, sid=None):
        """One block of live audio from the phone: 16 kHz mono int16 bytes.
        sid identifies one tap of TALK; seq numbers the blocks within it.
        The first block starts a talk session; end=True finishes it after the
        backlog has played. Returns (ok, message).

        10d bug: a new tap arriving while the previous one was still playing
        out was treated as the OLD session, its blocks (seq 0, 1, 2...) looked
        older than the old session's last seq, and were thrown away - "hello
        1 2 3" came out as "1 2 3". Now a new sid takes over the running
        session and plays straight after whatever is still buffered."""
        now = time.monotonic()
        x = pcm16_to_float(raw) if raw else np.zeros(0)

        with self._live_feed_lock:          # one live request at a time: no start races
            if self.busy and self.mode != "live":
                return False, "busy playing something else"

            # previous session has stopped taking audio and is winding down:
            # let it finish (at most one chunk, ~31 ms), then start fresh
            if self.busy and not self._live_active:
                t0 = time.monotonic()
                while self.busy and time.monotonic() - t0 < 1.0:
                    time.sleep(0.005)

            with self._live_lock:
                if sid is not None and sid in self._live_old_sids:
                    self.late_blocks += 1
                    return True, "block from a finished tap dropped"
                if self.busy and self._live_active:
                    if sid is not None and sid != self._live_sid:
                        if end:
                            return True, "end for an old tap ignored"
                        if self._live_sid is not None:
                            self._retire_sid(self._live_sid)
                        self._live_sid = sid             # new tap takes over
                        self._live_seq = -1
                        self._live_ended = False
                    if seq is not None and seq <= self._live_seq:
                        self.late_blocks += 1
                        return True, "late block dropped"
                    buf = np.concatenate([self._live_buf, x])
                    if len(buf) > LIVE_MAX_BUFFER:
                        buf = buf[-LIVE_PREBUFFER:]      # fell behind: jump to the present
                    self._live_buf = buf
                    self._live_last = now
                    if seq is not None:
                        self._live_seq = seq
                    if end:
                        self._live_ended = True
                    return True, "fed"

            if self.busy:
                return False, "still finishing the last sound"
            if end and len(x) == 0:
                return True, "nothing to end"

            # new session
            with self._live_lock:
                if self._live_sid is not None and self._live_sid != sid:
                    self._retire_sid(self._live_sid)
                self._live_buf = x
                self._live_last = now
                self._live_ended = bool(end)
                self._live_seq = seq if seq is not None else -1
                self._live_sid = sid
                self._live_active = True
            ok, msg = self._start_live()
            if not ok:
                with self._live_lock:
                    self._live_active = False
            return ok, msg

    def _retire_sid(self, sid):
        self._live_old_sids.append(sid)
        if len(self._live_old_sids) > 20:
            self._live_old_sids.pop(0)

    def _start_live(self):
        shaper = Shaper(self.level, init_peak=0.1, smooth=self.smooth, power=self.power,
                        clarity=self.clarity)
        self._shaper = shaper
        state = {"buffering": True, "need": LIVE_PREBUFFER}
        self.dropouts = 0
        self.late_blocks = 0
        silence = np.zeros(CHUNK)

        def source():
            with self._live_lock:
                n = len(self._live_buf)
                self.buffer_ms = int(n * 1000 / RATE)
                quiet_for = time.monotonic() - self._live_last
                over = self._live_ended or quiet_for > LIVE_TIMEOUT
                if state["buffering"]:
                    if n >= state["need"] or (over and n > 0):
                        state["buffering"] = False
                    elif over:
                        self._live_active = False
                        return None
                    else:
                        return silence
                if n >= CHUNK:
                    seg = self._live_buf[:CHUNK]
                    self._live_buf = self._live_buf[CHUNK:]
                elif over:
                    if n == 0:
                        self._live_active = False
                        return None
                    seg = np.concatenate([self._live_buf, np.zeros(CHUNK - n)])
                    self._live_buf = np.zeros(0)
                else:
                    state["buffering"] = True        # ran dry: re-buffer, play silence meanwhile
                    state["need"] = LIVE_REBUFFER
                    self.dropouts += 1
                    return silence
            out = shaper.process(seg)
            self.avg_drive = shaper.avg_drive
            return out

        return self._start(source, "live", "talking")

    # ── internals ────────────────────────────────────────────────────────────
    def _start(self, source, mode, msg):
        with self._lock:
            if self.busy:
                return False, f"busy ({self.mode})"
            self.busy = True
            self.mode = mode
            self._abort = False
            self.last_error = None
        if self.stop_motors:
            try:
                self.stop_motors()
            except Exception as e:
                print(f"[VOICE] stop_motors failed: {e}", flush=True)
        threading.Thread(target=self._worker, args=(source,), daemon=True).start()
        return True, msg

    def _worker(self, source):
        try:
            self.underruns = self._stream(source)
            print(f"[VOICE] {self.mode} done: underruns {self.underruns} "
                  f"dropouts {self.dropouts} late {self.late_blocks} relinks {self.relinks} "
                  f"work {self.work_ms:.1f} ms avg / {self.work_ms_max:.1f} ms max per 31 ms chunk "
                  f"({'fast' if self._fast else 'slow'} wave path)", flush=True)
        except Exception as e:
            self.last_error = str(e)
            print(f"[VOICE] error: {e}", flush=True)
        finally:
            self._live_active = False
            self._shaper = None
            self.mode = None
            self.busy = False

    def _add_wave(self, duty, dither):
        """Queue one chunk of pulses in pigpio."""
        rows = build_rows(duty, self.fwd_mask, self.back_mask, dither,
                          fixed62=self.fixed62, use_dither=self.use_dither)
        data = rows.astype("<u4").tobytes()
        if self._fast and not self._fast_checked:
            # Once per run: our bytes must be exactly what pigpio itself
            # would send for these pulses (struct "III" per pulse).
            ref = bytearray()
            for a, b, c in rows.tolist():
                ref.extend(struct.pack("III", a, b, c))
            self._fast_checked = True
            if bytes(ref) != data:
                self._fast = False
                print("[VOICE] fast wave path disagrees with pigpio's format - using the slow path",
                      flush=True)
        if self._fast:
            try:
                pigpio._u2i(pigpio._pigpio_command_ext(
                    self.pi.sl, pigpio._PI_CMD_WVAG, 0, 0, len(data), [data]))
                return
            except TypeError as e:
                # The internal call takes different arguments in this pigpio
                # version. A TypeError is raised before anything is sent, so
                # it's safe to fall back and send this chunk the normal way.
                self._fast = False
                print(f"[VOICE] fast wave path unavailable ({e}) - using the slow path", flush=True)
        if not self._fast:
            self.pi.wave_add_generic([pigpio.pulse(a, b, c) for a, b, c in rows.tolist()])

    def _create_wave(self):
        """Create the wave from the added pulses, padded to a fixed size so
        pigpio can always reuse a deleted wave's memory (11l)."""
        if self._pad:
            try:
                return self.pi.wave_create_and_pad(WAVE_PAD_PCT)
            except Exception as e:
                # Only a daemon too old to know the command falls back (the
                # added pulses are still there, so a plain create works). Any
                # other failure is real and stops the playback.
                if "unknown command" not in str(e).lower():
                    raise
                self._pad = False
                print(f"[VOICE] pigpio daemon has no wave_create_and_pad - using plain wave_create", flush=True)
        return self.pi.wave_create()

    def _stream(self, source):
        """Feed chunks to pigpio back to back (double buffered, 11j).

        The NEXT chunk is always built before the current one starts, and is
        linked behind it (SYNC) the moment the current one starts - with ~29 ms
        of playtime left to spare. 11i and earlier built the next chunk AFTER
        the current one started and linked it late; whenever the link landed
        just as the current wave ended, the new wave never started and the loop
        sat out a 0.5 s timeout (11 Nov: a 33 s clip took 84 s, 113 gaps).
        If a link is lost anyway (nothing playing, queued wave not started),
        the queued wave is started directly - a ~3 ms gap, not 500 ms."""
        pi = self.pi
        pi.wave_clear()
        underruns = 0               # moments with nothing playing
        relinks = 0                 # lost SYNC links that had to be restarted
        dither = [0.0]
        work = [0.0, 0]
        self.work_ms_max = 0.0
        self.relinks = 0

        def make():
            """Build the next chunk's wave. None = no more audio."""
            t = time.monotonic()
            duty = source()
            if duty is None:
                return None
            self._add_wave(duty, dither)
            wid = self._create_wave()
            w = (time.monotonic() - t) * 1000.0
            work[0] += w
            work[1] += 1
            self.work_ms = work[0] / work[1]
            if w > self.work_ms_max:
                self.work_ms_max = w
            return wid

        cur = None
        try:
            cur = make()
            if cur is None:
                return 0
            pi.wave_send_using_mode(cur, pigpio.WAVE_MODE_ONE_SHOT)
            nxt = None if self._abort else make()
            while nxt is not None and not self._abort:
                # cur is playing: queue nxt behind it straight away
                if pi.wave_tx_busy():
                    pi.wave_send_using_mode(nxt, pigpio.WAVE_MODE_ONE_SHOT_SYNC)
                else:
                    underruns += 1      # cur already finished: start nxt now
                    pi.wave_send_using_mode(nxt, pigpio.WAVE_MODE_ONE_SHOT)
                t0 = time.monotonic()
                while pi.wave_tx_at() != nxt and not self._abort:
                    if not pi.wave_tx_busy():
                        # Nothing playing and nxt hasn't started: the link was
                        # lost. Re-check once (it may have just started), then
                        # start it directly.
                        time.sleep(0.001)
                        if pi.wave_tx_at() != nxt and not pi.wave_tx_busy():
                            relinks += 1
                            underruns += 1
                            pi.wave_send_using_mode(nxt, pigpio.WAVE_MODE_ONE_SHOT)
                            t0 = time.monotonic()
                    if time.monotonic() - t0 > 0.5:
                        break           # safety: never hang here
                    time.sleep(0.001)
                pi.wave_delete(cur)
                cur = nxt
                self.relinks = relinks
                nxt = make()            # build the next one while cur plays
            if not self._abort:
                while pi.wave_tx_busy():
                    time.sleep(0.005)
        finally:
            try:
                pi.wave_tx_stop()
                pi.wave_clear()
            finally:
                for g in self.pins:
                    pi.write(g, 0)
        self.relinks = relinks
        return underruns
