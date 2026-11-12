#!/usr/bin/env python3
"""
ZeroBot Robot Server
====================
Flask web server that handles:
- Serving the phone control interface
- Receiving joystick/button commands via HTTP
- Controlling DC motors via TB6612 motor driver (GPIO)
- Toggling headlight LED via GPIO
- Reading battery voltage via I2C (ADS1115 ADC)

WIRING — ZeroBot carrier board (Rev F/G), from 2026-11-05b:
------------------------------------------------------------------
Motor driver TB6612 (on the carrier):
  AIN1 / AIN2  →  GPIO 17 / 27   (left motor forward / backward)
  BIN1 / BIN2  →  GPIO 22 / 23   (right motor forward / backward)
  PWMA, PWMB, STBY tied to 3.3 V on the carrier.

Headlight, white  →  GPIO 24 (physical pin 18). Carrier transistor Q1,
                     1k base resistor, NPN low-side: HIGH = on.
Headlight, RGB    →  GPIO 12 / 16 / 26 = red / green / blue (physical
                     32 / 36 / 37), transistors Q2-Q4, also HIGH = on.
                     Common-anode LEDs on 5 V, each colour switched low-side
                     by its transistor, so gpiozero active_high=True.
                     Only fitted on the RGB kit; harmless when absent.
GPIO 12/16/24/26 all default to pull-DOWN at boot, so every light stays off
until the program drives it. (The old GPIO 8 headlight defaulted to pull-UP.)

Battery sense: ADS1115 A0 sits on a 10k/10k divider from the switched pack,
so the pin reads HALF the pack voltage. BATTERY_SCALE = 2.0 below.

Earlier hand-wired builds (L9110S, headlight on GPIO 8, no divider) are not
supported by this build - their notes are in the guide's history.
------------------------------------------------------------------
"""

from flask import Flask, render_template, request, jsonify, Response
from version import STAMP, VERSION, BUILD
import collections
import colorsys
import json
import subprocess
import os
import signal
import sys

# ─── MOTORS AND HEADLIGHT VIA GPIOZERO ───────────────────────────────────────
# gpiozero Motor class works perfectly with L9110S H-bridge.
# forward/backward pin assignments match the L9110S wiring above.
# ─── CHANNEL SWAPS ────────────────────────────────────────────────────────────
# SWAP_MOTORS: set True when the robot drives forward correctly but steers the
# wrong way. Forward-correct plus steering-mirrored cannot be a rotation - only
# a reflection - and the reflection is the two motors sitting on each other's
# driver channels. Fixed here rather than by negating the joystick's x, so the
# tank mixing stays honest and each encoder still belongs to its own motor.
#
# SWAP_ENCODERS: set True if the balance bias runs straight to its limit
# instead of settling. That is positive feedback, and it means the encoder
# pins are crossed relative to the motors. Check with GET /balance.
SWAP_MOTORS   = False
SWAP_ENCODERS = False

# INVERT_A / INVERT_B: reverse one DRIVER CHANNEL. On a differential drive the
# two motors are mirror images, so identical wiring makes one of them run
# backwards. The symptom is "forward makes it spin" - and that is NOT a stick
# rotation, even though rotating the stick 90 degrees appears to cure it (it
# just commands a spin that the reversed motor turns into a straight line).
# Added 8 Sep 2026 after CONTROL_ROTATION = 270 was found to be doing exactly
# that. The flags belong to the channel, not to left/right, so toggling
# SWAP_MOTORS afterwards does not disturb them.
# 6 Nov 2026, Pi 3B+ on the Rev F carrier: throttle AND steering were both
# reversed, i.e. both motors ran backwards, so both channel flags flipped
# (was A True / B False on the hand-wired L9110S build).
INVERT_A = False
INVERT_B = True

_PINS_A = dict(forward=17, backward=27)   # L9110S channel A
_PINS_B = dict(forward=22, backward=23)   # L9110S channel B

def _chan(pins, invert):
    return dict(forward=pins["backward"], backward=pins["forward"]) if invert else dict(pins)

_CH_A = _chan(_PINS_A, INVERT_A)
_CH_B = _chan(_PINS_B, INVERT_B)

HEADLIGHT_PIN = 24              # carrier Rev F/G white headlight (was GPIO 8 on the hand-wired build)
RGB_PINS = (12, 16, 26)         # red, green, blue (RGB kit only)
BATTERY_SCALE = 2.0             # carrier 10k/10k divider: pack volts = pin volts x 2

try:
    from gpiozero import Motor, PWMLED
    if SWAP_MOTORS:
        motor_left  = Motor(**_CH_B)
        motor_right = Motor(**_CH_A)
    else:
        motor_left  = Motor(**_CH_A)
        motor_right = Motor(**_CH_B)
    # PWMLED, not LED: the headlight is dimmable now. 1 kHz is chosen against
    # the CAMERA, not the eye - slow PWM shows up as bands across the video
    # when the exposure is short in bright light. Raise it if you ever see
    # banding; lower it if software PWM costs too much CPU.
    headlight   = PWMLED(HEADLIGHT_PIN, frequency=1000)
    GPIO_AVAILABLE = True
    print("[OK] Motors and LED initialized via GPIO"
          + (" (channels swapped)" if SWAP_MOTORS else "")
          + (" (A inverted)" if INVERT_A else "")
          + (" (B inverted)" if INVERT_B else ""))
except Exception as e:
    GPIO_AVAILABLE = False
    print(f"[WARN] GPIO not available ({e}) - running in UI-only mode")

# RGB headlight, kept separate so an RGB problem can never take the motors down.
# One PWMLED per colour at 1 kHz - the same frequency as the white headlight,
# chosen to keep PWM bands out of the video. PWM is what lets the HUE slider
# pick any colour and the LUM slider dim it. The carrier's transistors switch
# each colour low-side, so HIGH = on (the PWMLED default).
rgb_leds = None
if GPIO_AVAILABLE:
    try:
        rgb_leds = [PWMLED(p, frequency=1000) for p in RGB_PINS]
        for _led in rgb_leds:
            _led.off()
        print(f"[OK] RGB headlight on GPIO {RGB_PINS[0]}/{RGB_PINS[1]}/{RGB_PINS[2]}")
    except Exception as e:
        rgb_leds = None
        print(f"[WARN] RGB headlight unavailable ({e})")

# ─── BATTERY VOLTAGE (ADS1115) ────────────────────────────────────────────────
# Required, not optional: the pack side is hard-wired, so the boost and the
# driver idle on the cells permanently and this readout is the only instrument
# that shows whether that drain matters.
ADC_ERROR = "not initialised"
try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
    i2c_ads = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c_ads)
    # ADS.P0 moved between library versions; in some it is an enum in
    # ads1x15, and underneath it is simply channel 0. Try each in turn rather
    # than pinning the kit to one release.
    try:
        _p0 = ADS.P0
    except AttributeError:
        try:
            # Current releases name the pins A0-A3, not P0-P3.
            from adafruit_ads1x15.ads1x15 import Pin as _AdsPin
            _p0 = getattr(_AdsPin, "P0", None) or _AdsPin.A0
        except Exception:
            _p0 = 0
    # Gain 2/3 gives 6.144 V full scale. At the default gain 1 the ceiling is
    # 4.096 V, and a full 1S pack sits at 4.2 V - so a charged pack CLIPS at
    # 32767 counts and reads a constant 4.096 V. That clipped reading, times
    # the old x2, is the "about 8 V" that looked like a divider fault.
    ads.gain = 2 / 3
    battery_channel = AnalogIn(ads, _p0)
    ADC_AVAILABLE = True
    ADC_ERROR = None
    print("[OK] ADS1115 found - battery voltage active")
except Exception as e:
    ADC_AVAILABLE = False
    ADC_ERROR = f"{type(e).__name__}: {e}"
    # Say WHY. "Not found" alone sends you looking at wiring when the cause is
    # usually software: I2C disabled in raspi-config, or Blinka not installed.
    print(f"[WARN] ADS1115 unavailable - battery will show N/A")
    print(f"       Reason: {ADC_ERROR}")
    print(f"       Check:  sudo raspi-config nonint get_i2c   (0 = enabled)")
    print(f"               i2cdetect -y 1                     (expect 48)")

# ─── SPEED (0.0 to 1.0) ───────────────────────────────────────────────────────
current_speed = 0.7  # Default 70% speed

# ─── STRAIGHT-LINE BALANCE ────────────────────────────────────────────────────
# Cheap TT gearmotors differ 10-20% in speed at the same PWM, so the robot
# pulls to one side with the stick centred. Rather than hand-calibrating a trim
# constant per robot - which does not scale past one unit, and drifts as the
# battery sags - the encoders close the loop and the correction finds itself.
#
# Single channel only. H1 on each motor is counted; H2 is wired but unused.
# Direction comes from the commanded sign, which is known, so the second
# channel buys nothing here and doubles the interrupt load.
# ON as of 8 Sep 2026, after GET /paircheck returned CORRECT on hardware:
# driving left alone gave 781 pulses on the left encoder and 0 on the right,
# driving right alone gave 874 and 6. Each encoder follows its own motor, so
# the feedback sign is right and the loop removes the error instead of driving
# it. Do not re-enable this on a rebuilt robot without re-running /paircheck.
# If the encoders are ever removed, balance_loop's starved detector switches
# this off by itself and says so in the log.
BALANCE_ENABLED = True

# Used whenever the closed loop is off. Trim the FASTER side DOWN.
TRIM_LEFT  = 1.00
TRIM_RIGHT = 1.00

BALANCE_LIMIT = 0.30   # hard cap on the correction, +/- fraction
BALANCE_GAIN  = 0.60   # INTEGRAL gain. Was 0.60 with no proportional term, which
                       # made the loop hunt: an integrator only responds after
                       # error has accumulated, so it overshoots, unwinds, and
                       # overshoots the other way. The zigzag widened with speed
                       # because the same bias error becomes more heading change
                       # the faster you go. Lowered when P was added 12 Sep 2026.
BALANCE_P     = 0.00   # PROPORTIONAL gain, OFF by default. Available as a knob,
                       # but simulation on 12 Sep 2026 showed the integrator does
                       # NOT hunt at these gains - it only goes unstable at a
                       # measurement lag around 1.5s, which is not happening. Do
                       # not turn this on to chase a zigzag until /balance/history
                       # shows the bias actually swinging. Acts on the error NOW, which is what
                       # damps the oscillation. Raise for a firmer hold, lower if
                       # it feels twitchy. P alone cannot remove a steady offset -
                       # that is the integral's job - so the two work together.
BALANCE_MIN_RATE = 20.0  # pulses/sec REQUIRED ON BOTH SIDES before a sample is
                         # allowed to teach the loop anything. The old guard only
                         # skipped when BOTH sides were near zero, so a spin-up
                         # sample of 10 and 0 passed and produced err = -2.0 -
                         # roughly fifteen times a normal error, slamming the
                         # integrator by 0.12 in a single step. Measured in the
                         # 12 Sep 2026 history capture as a bias swing of 0.24.
BALANCE_ERR_CLAMP = 0.5  # belt and braces: no single sample may claim a mismatch
                         # larger than this. A real one is around 0.13.
BALANCE_SEED = 0.0       # Starting value for the integrator, learned from the
                         # last run and rewritten as it converges. The motor
                         # mismatch is a physical property of the hardware - it
                         # is the same every time the robot is switched on - so
                         # relearning it from zero on every launch just means
                         # veering for the first second while the loop catches
                         # up. That was the "especially at the start" complaint
                         # on 12 Sep 2026. Saved at most once a minute, and only
                         # when it has actually moved, to spare the SD card.
BALANCE_SETTLE = 0.6     # seconds to ignore after any change in command. During
                         # acceleration the wheels are not yet at commanded speed,
                         # so rate/command is meaningless.
BALANCE_STRAIGHT = 0.15  # only learn when the two commands are within this
                         # fraction of each other. Mid-turn the inner wheel slips
                         # and normalising by command cannot account for it.
BALANCE_SMOOTH = 0.6   # error smoothing, 0 = none, 0.9 = heavy. The inner wheel
                       # command can be small during a turn and rate/command then
                       # amplifies pulse-count noise, so some smoothing is needed.
BALANCE_FLOOR = 0.15   # below this commanded magnitude, do not integrate

# Steering authority. 1.0 = original, spins on the spot alarmingly fast.
# Lower is calmer; raise if it will not turn tightly enough.
# 0.55 was still twitchy on hardware. Note this compounds with MOTOR_MIN
# below: the stiction remap lifts any nonzero command to at least 0.35, so a
# light touch on the stick already commands a third of full power.
TURN_SCALE = 1.0
# 1.0 from 6 Nov 2026: motors now run straight off the 1S pack (no motor
# boost), and at 0.5 the robot could not spin on the spot; at 1.0 it turned
# nicely and the Pi did not brown out. History below is from the boosted build.
# 0.12, chosen on the floor 8 Sep 2026. The earlier 0.30 was never a fair
# reading: it was tuned while MOTOR_MIN = 0.35 was inflating every steering
# command, so it was compensating for the deadband rather than setting a
# turn rate. Retune this if MOTOR_MIN changes again.

# Stiction deadband. Below this PWM these gearmotors hum but do not turn.
# Applied to the LARGER wheel command, with the smaller one scaled by the same
# factor, so a curve stays a curve. Tune 0.25-0.45 on the bench: too low and
# the dead zone returns, too high and there is no slow-speed control.
# (Max K's original adds 90/255 = 0.35 per wheel.)
MOTOR_MIN = 0.16

# Steering trim for CLOSED-LOOP mode. TRIM_LEFT/TRIM_RIGHT are plain output
# multipliers and the balance loop simply learns them away: it drives the two
# wheels to equal encoder speed, so a static multiplier is an error to be
# cancelled, not a heading preference to be honoured.
#
# This one is honoured, because it moves the loop's TARGET instead of its
# output. The loop drives its error to zero; adding a constant to that error
# makes it converge on a deliberate speed offset between the wheels and hold
# it. Needed because equal encoder pulses is not the same as straight -
# unequal tyre diameter or a dragging wheel curves a robot the encoders think
# is perfect.
#
# Same sign convention as the slider and as TRIM_LEFT: NEGATIVE slows the left
# wheel (correcting a robot that pulls right).
BALANCE_TRIM = 0.0
# 0.16, not the old 0.35. Both motors were measured breaking loose at 0.12 by
# GET /stallcheck on 8 Sep 2026 (left 8 pulses, right 12, at the third ramp
# step); 0.16 is that plus margin for a sagging battery. The 0.35 was measured
# on the PLAIN TT motors and silently survived the swap to geared Hiwonders,
# where it put a floor under the whole speed slider: 10% on the slider still
# commanded 0.415, so the bottom half of the slider did almost nothing, and
# every small steering input was inflated to a pivot.

# ── PERSISTED TUNING ──────────────────────────────────────────────────────
# Kept OUTSIDE the project directory on purpose: deploying does
# `rm -rf ~/zerobot`, which would delete a file stored alongside the code, and
# the watchdog can restart the service at any moment. Tuning that evaporates
# on restart is worse than no tuning at all - you test a value you are no
# longer running and draw the wrong conclusion. That happened on 8 Sep 2026.
TUNE_FILE = os.path.expanduser("~/.zerobot_tune.json")

_balance_bias = 0.0    # >0 means "left needs more", applied symmetrically
_balance_i    = 0.0    # integral term alone; _balance_bias is P + I, clamped
_seed_saved_at = 0.0   # last time BALANCE_SEED was written, for rate limiting
_err_smooth   = 0.0    # smoothed error, so noise does not reach the P term

# Rolling record of the last ~20s of loop samples. Added 12 Sep 2026 because a
# zigzag has two completely different explanations that feel identical from the
# driving seat: the loop swinging its correction, or the loop holding steady
# while something mechanical or in the steering input weaves. Guessing between
# them wasted a round trip. This shows which.
_balance_log = collections.deque(maxlen=200)
_cmd_changed_at = 0.0   # when the commanded speeds last moved, for BALANCE_SETTLE
_speed_ratio  = None   # smoothed left/right speed per unit command; >1 left faster

# ── SELF-CHECK WHILE DRIVING (7 Nov 2026) ─────────────────────────────────
# The robot checks its own encoders during normal driving, so nobody has to
# run /paircheck by hand on a new build. Three things are watched:
#   silent  - one wheel is driven hard but its encoder counts nothing
#   crossed - in a turn, the wheel told to go FASTER counts SLOWER
#   pinned  - the correction sits at its limit (crossed, or a dragging wheel)
# Any of them switches the correction off (the drive falls back to the TRM
# trims) and says why on the BAL button. It never swaps the encoders on its
# own - a false reading on a bumpy floor would then cause the circle problem.
# The swap is one tap on BAL, after the robot has said why.
_enc_swap   = False        # runtime encoder swap, saved as "swap_encoders"
_bal_fault  = None         # plain-English reason the loop switched off, or None
_bal_fault_kind = None     # "silent" | "crossed" | "pinned"
_pair_state = "unchecked"  # becomes "confirmed" after enough clear turns
_out_left = 0.0            # what actually went to the motors, after correction
_out_right = 0.0
_chk = {"starved": 0, "dead_l": 0, "dead_r": 0, "agree": 0,
        "disagree": 0, "pinned": 0}

CHECK_SILENT_N   = 20    # 2 s of one wheel counting while the other is silent
CHECK_TURN_RATIO = 1.5   # outputs must differ this much to count as a turn
CHECK_TURN_N     = 20    # turn samples per verdict (2 s of turning in total)
CHECK_TURN_VOTE  = 16    # this many of them must agree for a verdict
CHECK_PINNED_N   = 30    # 3 s of straight driving with the correction maxed

_TUNE_KEYS = ("motor_min", "turn_scale", "trim_left", "trim_right", "balance_enabled",
              "balance_p", "balance_i", "balance_smooth", "balance_seed",
              "balance_trim", "swap_encoders")
_TUNE_DEFAULTS = {"motor_min": MOTOR_MIN, "turn_scale": TURN_SCALE,
                  "trim_left": TRIM_LEFT, "trim_right": TRIM_RIGHT,
                  "balance_enabled": BALANCE_ENABLED,
                  "balance_p": BALANCE_P, "balance_i": BALANCE_GAIN,
                  "balance_smooth": BALANCE_SMOOTH, "balance_seed": BALANCE_SEED,
                  "balance_trim": BALANCE_TRIM, "swap_encoders": False}

def load_tune():
    """Apply saved tuning over the defaults, loudly.

    Logs every override at startup. A silent override is a trap: months later
    the constants in this file would not describe the running robot."""
    global MOTOR_MIN, TURN_SCALE, TRIM_LEFT, TRIM_RIGHT, BALANCE_ENABLED
    global BALANCE_P, BALANCE_GAIN, BALANCE_SMOOTH, BALANCE_SEED, BALANCE_TRIM
    global _enc_swap
    if not os.path.exists(TUNE_FILE):
        print(f"[INFO] No saved tuning at {TUNE_FILE} - using values from robot.py")
        return
    try:
        with open(TUNE_FILE) as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[WARN] Could not read {TUNE_FILE}: {exc} - using defaults")
        return
    for key in _TUNE_KEYS:
        if key not in data:
            continue
        val = data[key]
        if key == "motor_min":        MOTOR_MIN = float(val)
        elif key == "turn_scale":     TURN_SCALE = float(val)
        elif key == "trim_left":      TRIM_LEFT = float(val)
        elif key == "trim_right":     TRIM_RIGHT = float(val)
        elif key == "balance_enabled":BALANCE_ENABLED = bool(val)
        elif key == "balance_p":      BALANCE_P = float(val)
        elif key == "balance_i":      BALANCE_GAIN = float(val)
        elif key == "balance_smooth": BALANCE_SMOOTH = float(val)
        elif key == "balance_seed":   BALANCE_SEED = float(val)
        elif key == "balance_trim":   BALANCE_TRIM = float(val)
        elif key == "swap_encoders":  _enc_swap = bool(val)
        if val != _TUNE_DEFAULTS[key]:
            print(f"[TUNE] {key} = {val}  (robot.py says {_TUNE_DEFAULTS[key]})")

def save_tune():
    with open(TUNE_FILE, "w") as fh:
        json.dump({"motor_min": MOTOR_MIN, "turn_scale": TURN_SCALE,
                   "trim_left": TRIM_LEFT, "trim_right": TRIM_RIGHT,
                   "balance_enabled": BALANCE_ENABLED,
                  "balance_p": BALANCE_P, "balance_i": BALANCE_GAIN,
                  "balance_smooth": BALANCE_SMOOTH, "balance_seed": BALANCE_SEED,
                  "balance_trim": BALANCE_TRIM, "swap_encoders": _enc_swap}, fh, indent=2)
_cmd_left = 0.0
_cmd_right = 0.0

# ─── MOTOR WATCHDOG ───────────────────────────────────────────────────────────
# If the phone falls off wifi mid-drive, no client-side stop can save us —
# the last command just keeps running. This thread stops the motors if no
# /move or /stop has arrived for WATCHDOG_TIMEOUT seconds while they're
# active. The browser sends a heartbeat every 250ms while the stick is
# held, so 0.6s means at least two heartbeats were lost — a real dropout,
# not jitter.
import time
import threading

# The browser sends every 50ms, so 0.35s is seven missed sends - a real
# dropout, not jitter - while cutting the worst-case runaway roughly in half.
WATCHDOG_TIMEOUT = 0.35
last_command_time = time.monotonic()
motors_active = False

def watchdog():
    global motors_active
    while True:
        time.sleep(0.1)
        if motors_active and (time.monotonic() - last_command_time) > WATCHDOG_TIMEOUT:
            if GPIO_AVAILABLE:
                motor_left.stop()
                motor_right.stop()
            motors_active = False
            print("[WATCHDOG] Command silence - motors stopped")

threading.Thread(target=watchdog, daemon=True).start()

# ─── ENCODERS ─────────────────────────────────────────────────────────────────
# pigpio dispatches edges from a C daemon, so the per-edge cost in Python is
# just the counter increment. That matters on a single-core ARMv6 that is also
# streaming video: single-channel counting on both motors is ~1,260 edges/sec
# at full speed, which is affordable here and would not be through gpiozero's
# event machinery.
ENC_LEFT_PIN, ENC_RIGHT_PIN = (13, 5) if SWAP_ENCODERS else (5, 13)   # H1 only
_enc_left = 0
_enc_right = 0
_enc_left_total = 0     # lifetime, for /balance - proves pulses are arriving
_enc_right_total = 0
ENCODERS_AVAILABLE = False
_pi = None

try:
    import pigpio
    # At boot the service can start a moment before pigpiod is accepting
    # connections. One try lost the encoders for the whole run (6 Nov 2026),
    # so retry for up to ~10 s before giving up.
    for _attempt in range(10):
        _pi = pigpio.pi()
        if _pi.connected:
            break
        _pi.stop()
        time.sleep(1)
    if not _pi.connected:
        raise RuntimeError("pigpiod not running")

    # _enc_swap routes each pin to the other counter, so a crossed pair is
    # fixed by one tap on BAL with no restart and no code edit.
    def _count_left(gpio, level, tick):
        global _enc_left, _enc_left_total, _enc_right, _enc_right_total
        if _enc_swap:
            _enc_right += 1
            _enc_right_total += 1
        else:
            _enc_left += 1
            _enc_left_total += 1

    def _count_right(gpio, level, tick):
        global _enc_left, _enc_left_total, _enc_right, _enc_right_total
        if _enc_swap:
            _enc_left += 1
            _enc_left_total += 1
        else:
            _enc_right += 1
            _enc_right_total += 1

    for _p in (ENC_LEFT_PIN, ENC_RIGHT_PIN):
        _pi.set_mode(_p, pigpio.INPUT)
        _pi.set_pull_up_down(_p, pigpio.PUD_UP)
    _cb_left  = _pi.callback(ENC_LEFT_PIN,  pigpio.RISING_EDGE, _count_left)
    _cb_right = _pi.callback(ENC_RIGHT_PIN, pigpio.RISING_EDGE, _count_right)
    ENCODERS_AVAILABLE = True
    print(f"[OK] Encoders on GPIO {ENC_LEFT_PIN}/{ENC_RIGHT_PIN} - closed-loop balance active")
except Exception as e:
    print(f"[WARN] Encoders unavailable ({e}) - driving open-loop, expect some pull")

# ─── VOICE (speech through the drive motors) ─────────────────────────────────
# voice.py turns audio into ~16 kHz PWM on the motor driver pins using pigpio
# DMA waves. While voice.busy is True the pins belong to it: /move and /stop
# step aside (see their first lines). Needs pigpiod, same as the encoders.
voice = None
if GPIO_AVAILABLE and _pi is not None and _pi.connected:
    try:
        import voice as _voice_mod
        def _voice_stop_motors():
            motor_left.stop()
            motor_right.stop()
        voice = _voice_mod.Voice(
            _pi,
            [(_PINS_A["forward"], _PINS_A["backward"]),
             (_PINS_B["forward"], _PINS_B["backward"])],
            stop_motors=_voice_stop_motors)
        print("[OK] Voice ready - the robot can talk through its motors")
    except Exception as e:
        voice = None
        print(f"[WARN] Voice unavailable ({e})")


def _set_fault(kind, reason, reset_seed=False):
    """Switch the correction off and record why. First fault wins."""
    global _bal_fault, _bal_fault_kind, _balance_bias, _balance_i, _err_smooth
    global BALANCE_SEED
    if _bal_fault is not None:
        return
    _bal_fault, _bal_fault_kind = reason, kind
    _balance_bias = _balance_i = _err_smooth = 0.0
    print(f"[WARN] Balance switched itself off: {reason}")
    # A seed learned from crossed feedback is garbage; do not start from it.
    if reset_seed and BALANCE_SEED != 0.0:
        BALANCE_SEED = 0.0
        try:
            save_tune()
        except Exception as exc:
            print(f"[WARN] Could not save balance seed: {exc}")


def _clear_checks():
    for k in _chk:
        _chk[k] = 0


def balance_loop():
    """Integrate the speed difference between the wheels into a correction.

    Works on pulses-per-unit-command rather than raw rate, so it holds through
    curves as well as straight lines. Skipped while spinning in place, where
    the two wheels are commanded in opposite directions and the ratio is
    meaningless."""
    global _balance_i, _enc_left, _enc_right
    dt = 0.1
    # Start from what the last run learned rather than from nothing.
    _balance_i = max(-BALANCE_LIMIT, min(BALANCE_LIMIT, BALANCE_SEED))
    while True:
        time.sleep(dt)
        if not ENCODERS_AVAILABLE:
            continue
        # Keep measuring even when disabled, so /balance shows what the loop
        # WOULD do. That is how you check the pairing without risking a circle.
        pulses_l, _enc_left = _enc_left, 0
        pulses_r, _enc_right = _enc_right, 0
        try:
            _balance_step(pulses_l, pulses_r, dt)
        except Exception as exc:          # never let one bad sample kill the loop
            print(f"[WARN] balance step failed: {exc}")


def _balance_step(pulses_l, pulses_r, dt):
    """One 0.1 s sample: self-check first, then the correction."""
    global _balance_bias, _balance_i, _err_smooth, ENCODERS_AVAILABLE, _speed_ratio
    global BALANCE_SEED, _seed_saved_at, _pair_state

    cl, cr = abs(_cmd_left), abs(_cmd_right)
    # /stop and the watchdog stop the motors without zeroing _cmd_*, so a
    # stale command must not look like "driving with no pulses".
    if not motors_active or cl < BALANCE_FLOOR or cr < BALANCE_FLOOR:
        _chk["starved"] = _chk["dead_l"] = _chk["dead_r"] = 0
        return
    # During acceleration the wheels are not yet at commanded speed.
    if time.time() - _cmd_changed_at < BALANCE_SETTLE:
        return

    rate_l = pulses_l / dt
    rate_r = pulses_r / dt

    # ── SELF-CHECK 1: is anything counting? (any motion, spins included) ──
    if rate_l < 5 and rate_r < 5:
        _chk["dead_l"] = _chk["dead_r"] = 0
        _chk["starved"] += 1
        if _chk["starved"] == CHECK_SILENT_N:
            if _enc_left_total < 50 and _enc_right_total < 50:
                # Never counted since boot: no encoders fitted (base kit).
                # Hide the loop entirely; TRM drives open-loop.
                ENCODERS_AVAILABLE = False
                _balance_bias = 0.0
                print("[INFO] No encoder pulses since boot - assuming no encoders; open-loop")
            else:
                _set_fault("silent", "neither encoder is counting")
        return
    _chk["starved"] = 0

    # ── SELF-CHECK 2: one wheel silent while the other counts ──
    # Only when the silent side is driven well above stiction; a weak inner
    # wheel that has simply not broken loose is not a fault.
    if rate_l < 5 and rate_r >= BALANCE_MIN_RATE and abs(_out_left) >= 0.25:
        _chk["dead_l"] += 1
    else:
        _chk["dead_l"] = 0
    if rate_r < 5 and rate_l >= BALANCE_MIN_RATE and abs(_out_right) >= 0.25:
        _chk["dead_r"] += 1
    else:
        _chk["dead_r"] = 0
    if _chk["dead_l"] >= CHECK_SILENT_N:
        _set_fault("silent", "left encoder is not counting (check its plug)")
    if _chk["dead_r"] >= CHECK_SILENT_N:
        _set_fault("silent", "right encoder is not counting (check its plug)")

    same_direction = (_cmd_left > 0) == (_cmd_right > 0)
    if not same_direction:
        return            # spins: equal and opposite, no pairing information

    # ── SELF-CHECK 3: pairing, judged in turns ──
    # The wheel given MORE power must count MORE. Uses the outputs actually
    # sent, so the correction itself cannot fake a verdict.
    ol, orr = abs(_out_left), abs(_out_right)
    hi, lo = max(ol, orr), min(ol, orr)
    if hi >= 0.3 and hi >= CHECK_TURN_RATIO * max(lo, 1e-6):
        left_faster_cmd = ol > orr
        if rate_l > 1.2 * rate_r:
            vote = "agree" if left_faster_cmd else "disagree"
        elif rate_r > 1.2 * rate_l:
            vote = "disagree" if left_faster_cmd else "agree"
        else:
            vote = None
        if vote:
            _chk[vote] += 1
            if _chk["agree"] + _chk["disagree"] >= CHECK_TURN_N:
                if _chk["disagree"] >= CHECK_TURN_VOTE:
                    _pair_state = "unchecked"
                    _set_fault("crossed", "encoders look crossed - tap BAL to swap them",
                               reset_seed=True)
                elif _chk["agree"] >= CHECK_TURN_VOTE:
                    if _pair_state != "confirmed":
                        print("[OK] Encoder pairing confirmed while driving")
                    _pair_state = "confirmed"
                _chk["agree"] = _chk["disagree"] = 0

    # Only learn from steady, near-straight driving. Everything else -
    # acceleration, turns - produces a rate/command figure that does not
    # mean what the loop assumes it means.
    if abs(cl - cr) / max(cl, cr) > BALANCE_STRAIGHT:
        return
    # BOTH sides must be really turning. The old test was `and`, so one wheel
    # spinning while the other had not started passed straight through.
    if rate_l < BALANCE_MIN_RATE or rate_r < BALANCE_MIN_RATE:
        return

    norm_l = rate_l / cl      # pulses/sec per unit of command
    norm_r = rate_r / cr
    avg = (norm_l + norm_r) / 2.0
    if avg <= 0:
        return

    # Smoothed speed ratio. THIS is the honest measurement, and unlike the
    # integrator it stays meaningful whether the loop is acting or not.
    # >1 means left runs faster than right for the same command.
    inst = norm_l / norm_r if norm_r > 0 else 0.0
    _speed_ratio = inst if _speed_ratio is None else (0.85 * _speed_ratio + 0.15 * inst)

    # Do NOT integrate while disabled. The correction is not applied, so
    # nothing removes the error and the integrator always runs to its limit
    # - which made "at_limit" look like a crossed-encoder diagnosis when it
    # was simply an open integrator. Corrected 8 Sep 2026.
    if not BALANCE_ENABLED or _bal_fault is not None:
        _balance_bias = _balance_i = _err_smooth = 0.0
        _chk["pinned"] = 0
        return

    # BALANCE_TRIM shifts the target the loop converges on. At 0 it
    # settles at equal wheel speed; at -0.08 it settles holding the left
    # wheel ~8% slower, and HOLDS it instead of correcting it away.
    err = (norm_r - norm_l) / avg + BALANCE_TRIM   # >0: right runs faster
    err = max(-BALANCE_ERR_CLAMP, min(BALANCE_ERR_CLAMP, err))
    _err_smooth = BALANCE_SMOOTH * _err_smooth + (1.0 - BALANCE_SMOOTH) * err

    # PI. The integral is clamped on its own as well as the sum, so a long
    # straight run cannot wind up a term that then takes seconds to unwind
    # when the error reverses - which is what a zigzag actually is.
    _balance_i += BALANCE_GAIN * err * dt
    _balance_i = max(-BALANCE_LIMIT, min(BALANCE_LIMIT, _balance_i))

    _balance_bias = BALANCE_P * _err_smooth + _balance_i
    _balance_bias = max(-BALANCE_LIMIT, min(BALANCE_LIMIT, _balance_bias))

    # ── SELF-CHECK 4: correction maxed out on a straight ──
    # Crossed encoders run the integrator to its limit (positive feedback);
    # so does a wheel that drags badly. Either way the loop is not helping.
    if abs(_balance_bias) >= BALANCE_LIMIT - 1e-6:
        _chk["pinned"] += 1
        if _chk["pinned"] >= CHECK_PINNED_N:
            _set_fault("pinned", "correction maxed out - crossed encoders or a "
                       "dragging wheel. A few gentle turns will tell which.",
                       reset_seed=True)
            return
    else:
        _chk["pinned"] = 0

    # Persist the converged correction, gently. Only when it has genuinely
    # drifted from what is stored, and at most once a minute.
    now = time.time()
    if abs(_balance_i - BALANCE_SEED) > 0.01 and now - _seed_saved_at > 60:
        BALANCE_SEED = round(_balance_i, 4)
        _seed_saved_at = now
        try:
            save_tune()
        except Exception as exc:
            print(f"[WARN] Could not save balance seed: {exc}")

    _balance_log.append((round(time.time(), 4), round(err, 4),
                         round(_balance_bias, 4), round(rate_l, 1),
                         round(rate_r, 1), round(cl, 3), round(cr, 3)))


threading.Thread(target=balance_loop, daemon=True).start()

# ─── CAMERA STREAM ────────────────────────────────────────────────────────────
# Video is served by camera.py on its own port (8080), straight from the
# hardware JPEG encoder. Flask handles CONTROL ONLY and never touches a
# frame — the same architecture the original ZeroBot used, which is what
# makes streaming viable on a single-core Zero W.
import camera

# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/video")
def video():
    """Old URL kept for compatibility — points at the real stream."""
    host = request.host.split(":")[0]
    return (f'<html><body style="margin:0"><img src="http://{host}:8080/stream" '
            f'style="width:100%"></body></html>')

@app.route("/")
def index():
    """Serve the main control page.

    The stamp is baked into the HTML at render time, so a page the phone has
    cached carries the OLD stamp while /version returns the live one. That
    disagreement is what tells you to hard-refresh instead of hunting a
    phantom bug in code the browser never loaded."""
    return render_template("index.html", version=STAMP)

@app.route("/version")
def version():
    """Identity of the CONTROL server, plus its process id.

    The pid matters more than the stamp. camera.start() runs inside this very
    process, so :5000 and :8080 must report the SAME pid. Two different pids
    means two instances are running and the stream server you are watching
    belongs to the other one - the failure that cost an hour on 6 Sep, where
    allow_reuse_address let a second process bind 8080 and serve frames from
    code that had already been replaced."""
    return jsonify({"stamp": STAMP, "version": VERSION, "build": BUILD,
                    "pid": os.getpid(), "role": "control"})

@app.route("/move", methods=["POST"])
def move():
    """
    Receive joystick data from the phone.
    Expects JSON: { "x": -1.0 to 1.0, "y": -1.0 to 1.0 }
      x = left/right steering
      y = forward/backward throttle
    """
    global current_speed, last_command_time, motors_active
    # The robot is talking: the motor pins belong to voice.py until it is done.
    if voice is not None and voice.busy:
        return jsonify({"status": "speaking", "left": 0.0, "right": 0.0})
    data = request.get_json()
    x = float(data.get("x", 0))  # Left/right  (-1 = full left, 1 = full right)
    y = float(data.get("y", 0))  # Fwd/back    (-1 = full back, 1 = full forward)

    last_command_time = time.monotonic()

    # ── TANK STEERING MATH ──────────────────────────────────────────────────
    # Mix x and y into separate left/right motor speeds.
    # This is called "tank drive" - each side is controlled independently.
    # Examples:
    #   Joystick straight up (y=1, x=0)  -> both motors forward at full speed
    #   Joystick hard right (y=0, x=1)   -> left fwd, right backward (spin right)
    #   Joystick up-right (y=0.7, x=0.5) -> left faster than right (curve right)

    # Full-scale x spins the robot far faster than is useful for FPV driving,
    # where you want to place the nose, not pirouette. Scaling steering rather
    # than clamping it keeps fine control near centre.
    x *= TURN_SCALE

    left_speed  = (y + x) * current_speed
    right_speed = (y - x) * current_speed

    # ── STICTION DEADBAND, RATIO-PRESERVING ────────────────────────────────
    # Remap the larger wheel from [0..1] to [MOTOR_MIN..1] and scale the other
    # wheel by the same factor. Remapping each wheel separately (the old way)
    # lifted a 0.07 inner wheel to 0.40 while a 0.00 inner wheel stayed at 0 -
    # a cliff exactly where gentle steering lives.
    mag = max(abs(left_speed), abs(right_speed))
    if mag > 1.0:                       # clamp without changing the ratio
        left_speed  /= mag
        right_speed /= mag
        mag = 1.0
    if mag < 0.02:
        left_speed = right_speed = 0.0
    else:
        k = (MOTOR_MIN + (1.0 - MOTOR_MIN) * mag) / mag
        left_speed  *= k
        right_speed *= k

    # Hand the commanded values to the balance loop before correcting them,
    # so it measures against what was asked for, not against its own output.
    global _cmd_left, _cmd_right, _cmd_changed_at
    if abs(left_speed - _cmd_left) > 0.02 or abs(right_speed - _cmd_right) > 0.02:
        _cmd_changed_at = time.time()
    _cmd_left, _cmd_right = left_speed, right_speed

    if BALANCE_ENABLED and ENCODERS_AVAILABLE and _bal_fault is None:
        left_speed  = max(-1.0, min(1.0, left_speed  * (1.0 + _balance_bias)))
        right_speed = max(-1.0, min(1.0, right_speed * (1.0 - _balance_bias)))
    else:
        left_speed  = max(-1.0, min(1.0, left_speed  * TRIM_LEFT))
        right_speed = max(-1.0, min(1.0, right_speed * TRIM_RIGHT))

    global _out_left, _out_right
    _out_left, _out_right = left_speed, right_speed

    if GPIO_AVAILABLE:
        if left_speed > 0.05:     motor_left.forward(left_speed)
        elif left_speed < -0.05:  motor_left.backward(abs(left_speed))
        else:                     motor_left.stop()
        if right_speed > 0.05:    motor_right.forward(right_speed)
        elif right_speed < -0.05: motor_right.backward(abs(right_speed))
        else:                     motor_right.stop()

    # Watchdog only arms when the motors are actually commanded to move
    motors_active = (abs(left_speed) > 0.05 or abs(right_speed) > 0.05)

    return jsonify({"status": "ok", "left": left_speed, "right": right_speed})

@app.route("/stop", methods=["POST"])
def stop():
    """Emergency stop - called when joystick is released"""
    global motors_active
    motors_active = False
    if voice is not None and voice.busy:
        # gpiozero writing the pins mid-speech would chop the sound up; the
        # motors are already stopped as far as driving is concerned.
        return jsonify({"status": "speaking"})
    if GPIO_AVAILABLE:
        motor_left.stop()
        motor_right.stop()
    return jsonify({"status": "stopped"})

# ─── VOICE ROUTES ─────────────────────────────────────────────────────────────
# The TALK page (/talk) is meant to be opened over HTTPS on port 5443 - phones
# only give a web page the microphone on a secure connection. These routes
# work on both ports.
# Audio arrives as raw 16 kHz mono signed 16-bit little-endian PCM; the phone
# decodes and resamples, so the Pi never needs ffmpeg.

def _no_voice():
    return jsonify({"available": False, "error": "voice needs pigpiod and the motors"}), 503

@app.route("/talk")
def talk_page():
    return render_template("talk.html", version=STAMP)

@app.route("/voice", methods=["GET"])
def voice_status():
    if voice is None:
        return jsonify({"available": False})
    return jsonify(voice.status())

@app.route("/voice/level", methods=["POST"])
def voice_level():
    if voice is None:
        return _no_voice()
    data = request.get_json(silent=True) or {}
    if "smooth" in data:
        voice.set_smooth(data["smooth"])
    if "power" in data:
        voice.set_power(data["power"])
    if "level" in data:
        voice.set_level(data["level"])
    if "clarity" in data:
        voice.set_clarity(data["clarity"])
    if "fixed62" in data or "dither" in data:       # 11m tone-test switches
        voice.set_tone_test(fixed62=data.get("fixed62"), dither=data.get("dither"))
    return jsonify({"level": voice.level, "smooth": voice.smooth, "power": voice.power,
                    "clarity": voice.clarity, "fixed62": voice.fixed62, "dither": voice.use_dither})

@app.route("/voice/say", methods=["POST"])
def voice_say():
    if voice is None:
        return _no_voice()
    data = request.get_json(silent=True) or {}
    ok, msg = voice.say(data.get("text", ""))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

@app.route("/voice/clip", methods=["POST"])
def voice_clip():
    if voice is None:
        return _no_voice()
    raw = request.get_data(cache=False)
    if len(raw) < 2:
        return jsonify({"ok": False, "message": "no audio received"}), 400
    ok, msg = voice.play_clip(_voice_mod.pcm16_to_float(raw))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

@app.route("/voice/live", methods=["POST"])
def voice_live():
    if voice is None:
        return _no_voice()
    raw = request.get_data(cache=False)
    try:
        seq = int(request.args.get("seq", ""))
    except ValueError:
        seq = None
    end = request.args.get("end") == "1"
    sid = request.args.get("sid") or None
    ok, msg = voice.live_feed(raw, seq=seq, end=end, sid=sid)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

@app.route("/voice/stop", methods=["POST"])
def voice_stop():
    if voice is None:
        return _no_voice()
    voice.stop()
    return jsonify({"ok": True})

# Remembered so the button turns the light back on at the brightness you last
# chose, instead of jumping to full every time.
headlight_level = 100
headlight_state = False

# HUE slider: 0-359 is a colour around the wheel, RGB_WHITE_HUE and above is
# white (all three colours). Starts on white - it is a headlight first.
RGB_WHITE_HUE = 370
RGB_HUE_MAX = 390
rgb_hue = RGB_WHITE_HUE


def _hue_rgb(hue):
    if hue >= RGB_WHITE_HUE:
        return (1.0, 1.0, 1.0)
    return colorsys.hsv_to_rgb((hue % 360) / 360.0, 1.0, 1.0)


def _apply_lights():
    """Drive the white headlight AND the RGB headlight from one state.
    Base kit has no RGB fitted; RGB kit may have no white fitted. Driving both
    means the one button works on either."""
    duty = _headlight_duty(headlight_level) if headlight_state else 0.0
    if GPIO_AVAILABLE:
        headlight.value = duty
    if rgb_leds:
        for led, c in zip(rgb_leds, _hue_rgb(rgb_hue)):
            led.value = c * duty


def _headlight_duty(pct):
    """Percent from the slider -> PWM duty.

    Gamma-corrected: perceived brightness goes roughly as the 2.2 power of
    the light output, so a linear duty ramp feels like it does almost all its
    work in the bottom third of the slider. This makes the travel feel even.
    The floor keeps the lowest setting actually visible rather than black.
    """
    pct = max(0.0, min(100.0, float(pct)))
    if pct <= 0:
        return 0.0
    return max(0.02, (pct / 100.0) ** 2.2)


@app.route("/headlight", methods=["GET", "POST"])
def headlight_toggle():
    """Headlight on/off and brightness.

    POST {"on": bool} toggles at the remembered level.
    POST {"brightness": 0-100} sets the level; 0 turns it off, and any value
    above 0 turns it on, so dragging the slider is never a no-op.
    POST {"hue": 0-390} sets the RGB colour (370+ = white) and turns it on.
    Drives the white AND the RGB headlight together.
    """
    global headlight_level, headlight_state, rgb_hue
    if request.method == "GET":
        return jsonify({"headlight": headlight_state, "brightness": headlight_level,
                        "hue": rgb_hue, "rgb": rgb_leds is not None,
                        "white_hue": RGB_WHITE_HUE, "hue_max": RGB_HUE_MAX})

    data = request.get_json(silent=True) or {}
    try:
        if "hue" in data:
            rgb_hue = max(0, min(RGB_HUE_MAX, int(float(data["hue"]))))
            headlight_state = True
            if headlight_level <= 0:
                headlight_level = 100
        if "brightness" in data:
            headlight_level = max(0, min(100, int(float(data["brightness"]))))
            headlight_state = headlight_level > 0
        elif "on" in data:
            headlight_state = bool(data["on"])
            if headlight_state and headlight_level <= 0:
                headlight_level = 100     # toggled on from a zeroed slider
    except (TypeError, ValueError):
        return jsonify({"error": "brightness must be 0-100, hue 0-390"}), 400

    _apply_lights()
    return jsonify({"status": "ok", "headlight": headlight_state,
                    "brightness": headlight_level, "hue": rgb_hue})


# Named colours for /rgb, as positions on the HUE slider. None = off.
RGB_COLOURS = {"off": None, "red": 0, "yellow": 60, "green": 120, "cyan": 180,
               "blue": 240, "magenta": 300, "white": RGB_WHITE_HUE}


@app.route("/rgb", methods=["GET", "POST"])
def rgb_set():
    """RGB headlight colour (RGB kit only).

    POST {"colour": "red"} - one of off, red, green, blue, yellow, cyan,
    magenta, white. GET returns the current colour."""
    global headlight_state, rgb_hue, headlight_level
    if request.method == "GET":
        name = "off" if not headlight_state else next(
            (k for k, v in RGB_COLOURS.items() if v == rgb_hue), "custom")
        return jsonify({"colour": name, "hue": rgb_hue, "pins": list(RGB_PINS)})
    data = request.get_json(silent=True) or {}
    name = str(data.get("colour", data.get("color", ""))).lower()
    if name not in RGB_COLOURS:
        return jsonify({"error": "colour must be one of " + ", ".join(RGB_COLOURS)}), 400
    if rgb_leds is None:
        return jsonify({"error": "RGB headlight not available"}), 503
    if RGB_COLOURS[name] is None:
        headlight_state = False
    else:
        rgb_hue = RGB_COLOURS[name]
        headlight_state = True
        if headlight_level <= 0:
            headlight_level = 100
    _apply_lights()
    return jsonify({"status": "ok", "colour": name})

@app.route("/speed", methods=["POST"])
def set_speed():
    """Set motor speed multiplier (0.0 to 1.0)"""
    global current_speed
    data = request.get_json()
    current_speed = float(data.get("speed", 0.7))
    current_speed = max(0.1, min(1.0, current_speed))
    return jsonify({"status": "ok", "speed": current_speed})

@app.route("/balance")
def balance():
    """Read-only. Shows what the closed loop has settled on.

    bias > 0 means the left motor is being given more, i.e. the right one is
    naturally faster. A value parked at the limit means the mismatch is bigger
    than the loop can correct - look for a mechanical cause."""
    return jsonify({
        "state": _bal_state(),
        "fault": _bal_fault,
        "fault_kind": _bal_fault_kind,
        "pairing": _pair_state,
        "swap_encoders": _enc_swap,
        "enabled": BALANCE_ENABLED,
        "encoders": ENCODERS_AVAILABLE,
        "bias": round(_balance_bias, 4),
        # P reacts now, I holds the steady offset. If bias swings while the
        # robot is going straight, P is too low or I too high.
        "bias_p": round(BALANCE_P * _err_smooth, 4),
        "bias_i": round(_balance_i, 4),
        "gains": {"p": BALANCE_P, "i": BALANCE_GAIN, "smooth": BALANCE_SMOOTH},
        "seed": BALANCE_SEED,
        "at_limit": abs(_balance_bias) >= BALANCE_LIMIT - 1e-6,
        "trim": [TRIM_LEFT, TRIM_RIGHT],
        "pulses": [_enc_left_total, _enc_right_total],
        # ratio > 1: left runs faster. suggest_trim is the [LEFT, RIGHT] pair
        # that evens them up open-loop. Always trims the FASTER side DOWN and
        # leaves the slower at 1.00 - never boosts above 1.0, which would eat
        # headroom and clip at full throttle.
        "ratio": None if _speed_ratio is None else round(_speed_ratio, 3),
        "suggest_trim": None if not _speed_ratio else (
            [round(1.0 / _speed_ratio, 3), 1.0] if _speed_ratio > 1.0
            else [1.0, round(_speed_ratio, 3)]),
    })

def _bal_state():
    """One word for the BAL button: absent / fault / off / on."""
    if not ENCODERS_AVAILABLE:
        return "absent"
    if _bal_fault is not None:
        return "fault"
    return "on" if BALANCE_ENABLED else "off"

@app.route("/balance/control", methods=["POST"])
def balance_control():
    """Button actions. {"action": "clear"} retries after a fault;
    {"action": "swap"} swaps the encoders (saved) and retries."""
    global _bal_fault, _bal_fault_kind, _enc_swap, _pair_state
    global _balance_bias, _balance_i, _err_smooth, BALANCE_SEED
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("clear", "swap"):
        return jsonify({"error": "action must be clear or swap"}), 400
    if action == "swap":
        _enc_swap = not _enc_swap
        _pair_state = "unchecked"
        BALANCE_SEED = 0.0
        print(f"[INFO] Encoders swapped from the control page (swap_encoders = {_enc_swap})")
    _bal_fault = _bal_fault_kind = None
    _balance_bias = _balance_i = _err_smooth = 0.0
    _clear_checks()
    try:
        save_tune()
    except Exception as exc:
        return jsonify({"error": f"could not save: {exc}"}), 500
    return jsonify({"state": _bal_state(), "swap_encoders": _enc_swap})

@app.route("/balance/history")
def balance_history():
    """The last ~20 seconds of loop samples, plus the numbers that settle it.

    Read this after a run that misbehaved. The question it answers:

      bias_swing LARGE  -> the correction itself is moving. The loop is the
                           problem: look at noise in rate_l/rate_r, then at
                           BALANCE_GAIN.
      bias_swing SMALL  -> the loop is holding steady and the weave comes from
                           somewhere else: joystick jitter in the commanded
                           values (watch cmd_l/cmd_r), wheel slip, or chassis
                           geometry. Changing gains would be chasing the wrong
                           thing."""
    rows = list(_balance_log)
    if not rows:
        return jsonify({"samples": 0,
                        "note": "drive in a straight line for ~10s, then read this again"})
    bias = [r[2] for r in rows]
    err  = [r[1] for r in rows]
    cmds = [r[5] - r[6] for r in rows]     # commanded left-right difference
    def swing(v): return round(max(v) - min(v), 4)
    def mean(v):  return round(sum(v) / len(v), 4)
    verdict = ("bias is SWINGING - the loop is moving its own correction"
               if swing(bias) > 0.08 else
               "bias is STEADY - the weave is not coming from the loop")
    return jsonify({
        "samples": len(rows),
        "seconds": round(rows[-1][0] - rows[0][0], 1),
        "bias_swing": swing(bias), "bias_mean": mean(bias),
        "err_swing": swing(err),   "err_mean": mean(err),
        "cmd_diff_swing": swing(cmds),
        "verdict": verdict,
        "note": "cmd_diff_swing large on a straight run = joystick jitter, not the loop",
        "columns": ["t", "err", "bias", "rate_l", "rate_r", "cmd_l", "cmd_r"],
        "rows": rows,
    })

@app.route("/photo", methods=["POST"])
def photo():
    """One full-resolution still.

    SLOW BY DESIGN: this switches the sensor mode, so expect several seconds
    with no video. The UI must say so before the user assumes it has hung.
    """
    ok, result = camera.capture_still()
    if not ok:
        return jsonify({"error": result}), 503
    return jsonify({"status": "ok", "file": result,
                    "url": f"/recordings/{result}"})


@app.route("/focus", methods=["GET", "POST"])
def focus():
    """Manual focus in dioptres. 0 = infinity, 0.5 = 2 m, 4.0 = 25 cm.

    Lives on :5000, not on the camera's :8080, because camera.py runs INSIDE
    this process - a direct function call, no second HTTP hop and no CORS
    preflight from a page that was served from a different port.
    """
    if request.method == "GET":
        return jsonify(camera.focus_state())
    data = request.get_json(silent=True) or {}
    if "lens" not in data:
        return jsonify({"error": 'send {"lens": 0.0-10.0}'}), 400
    ok, result = camera.set_focus(data["lens"])
    if not ok:
        return jsonify({"error": result}), 503
    return jsonify({"status": "ok", "lens": result})


@app.route("/focus/auto", methods=["POST"])
def focus_auto():
    """One autofocus sweep, then LOCK. Never leaves continuous AF running.

    Blocks for up to ~3s waiting for the sweep to settle. Flask is threaded,
    so this holds one worker and not the drive endpoint.
    """
    ok, result = camera.autofocus_once()
    if not ok:
        return jsonify({"error": result,
                        "lens": camera.focus_state().get("lens")}), 503
    return jsonify({"status": "ok", "lens": result})


@app.route("/paircheck")
def paircheck():
    """Definitive encoder-to-motor pairing test. WHEELS OFF THE GROUND.

    Drives one motor at a time and reports which counter moved. This replaces
    watching the balance integrator, which cannot answer the question: the
    integrator winds to its limit whenever a steady speed difference exists,
    regardless of pairing, so its value carries no pairing information at all.

    Takes about four seconds and drives each side briefly."""
    global _enc_left, _enc_right, _cmd_left, _cmd_right, motors_active
    if not GPIO_AVAILABLE:
        return jsonify({"error": "GPIO unavailable"}), 503
    if not ENCODERS_AVAILABLE:
        return jsonify({"error": "encoders unavailable - nothing to pair"}), 503

    results = {}
    for side, mot in (("left", motor_left), ("right", motor_right)):
        motor_left.stop(); motor_right.stop()
        time.sleep(0.4)
        l0, r0 = _enc_left_total, _enc_right_total
        mot.forward(0.6)
        time.sleep(1.2)
        mot.stop()
        time.sleep(0.4)
        results[side] = {"enc_left": _enc_left_total - l0,
                         "enc_right": _enc_right_total - r0}
    motors_active = False
    _cmd_left = _cmd_right = 0.0

    dl, dr = results["left"], results["right"]
    lo = 20   # below this, treat the channel as not counting at all
    if max(dl["enc_left"], dl["enc_right"]) < lo or max(dr["enc_left"], dr["enc_right"]) < lo:
        verdict = "NO PULSES - an encoder is not wired, or a motor did not turn"
        action  = "check H1 on GPIO 5 and 13, and that both motors actually spun"
    elif dl["enc_left"] > dl["enc_right"] and dr["enc_right"] > dr["enc_left"]:
        verdict = "CORRECT - each encoder follows its own motor"
        action  = "nothing to do; balance is safe to use (BAL button)"
        global _pair_state
        _pair_state = "confirmed"
    elif dl["enc_right"] > dl["enc_left"] and dr["enc_left"] > dr["enc_right"]:
        verdict = "CROSSED - the encoders belong to the opposite motors"
        action  = ("POST /balance/control {\"action\": \"swap\"} (or let the BAL "
                   "button offer it), then run this again")
    else:
        verdict = "AMBIGUOUS - both counters moved for one motor"
        action  = "likely a shared wire or mechanical coupling; inspect before enabling"

    return jsonify({"driving_left": dl, "driving_right": dr,
                    "verdict": verdict, "action": action,
                    "swap_encoders": _enc_swap})

@app.route("/stallcheck")
def stallcheck():
    """Measure the real stiction threshold. WHEELS OFF THE GROUND.

    Ramps each motor from a crawl upward and watches its encoder for the first
    PWM at which the shaft actually turns. MOTOR_MIN was 0.35, measured on the
    plain TT motors; the geared Hiwonders fitted since need much less, and the
    old value was quietly setting a floor under the speed slider.

    Takes about fifteen seconds."""
    if not GPIO_AVAILABLE:
        return jsonify({"error": "GPIO unavailable"}), 503
    if not ENCODERS_AVAILABLE:
        return jsonify({"error": "encoders unavailable - cannot detect rotation"}), 503

    global _cmd_left, _cmd_right, motors_active
    hold, step = 0.40, 0.04
    levels = [round(0.04 + step * i, 3) for i in range(15)]   # 0.04 .. 0.60
    MOVING = 8            # pulses within `hold` that count as really turning

    out = {}
    for side, mot, counter in (("left", motor_left, "l"), ("right", motor_right, "r")):
        motor_left.stop(); motor_right.stop()
        time.sleep(0.3)
        trace, found = [], None
        for pwm in levels:
            before = _enc_left_total if counter == "l" else _enc_right_total
            mot.forward(pwm)
            time.sleep(hold)
            after = _enc_left_total if counter == "l" else _enc_right_total
            pulses = after - before
            trace.append({"pwm": pwm, "pulses": pulses})
            if pulses >= MOVING and found is None:
                found = pwm
                break         # stop as soon as it moves; no need to ramp further
        mot.stop()
        time.sleep(0.3)
        out[side] = {"stall_pwm": found, "trace": trace}

    motors_active = False
    _cmd_left = _cmd_right = 0.0

    both = [out[s]["stall_pwm"] for s in ("left", "right")]
    if None in both:
        suggested, note = None, ("one motor never turned up to 0.60 - check "
                                 "wiring, or that the wheel is free to spin")
    else:
        # Take the worse of the two and add a margin, so the stiffer motor
        # still starts reliably on a sagging battery.
        suggested = round(min(0.60, max(both) + 0.04), 2)
        note = (f"set MOTOR_MIN = {suggested} (worst side {max(both)} plus margin). "
                f"Was {MOTOR_MIN}. Try it live with POST /tune first.")
    return jsonify({"measured": out, "suggested_motor_min": suggested,
                    "current_motor_min": MOTOR_MIN, "note": note})

@app.route("/tune", methods=["GET", "POST"])
def tune():
    """Read or change the feel constants WITHOUT a redeploy.

    POST {"motor_min": 0.18, "turn_scale": 0.22}

    Values live in memory only and are lost on restart - that is deliberate, so
    a bad number can never strand the robot. Once a setting feels right, it has
    to be written into robot.py to survive.

    Included because TURN_SCALE and MOTOR_MIN can only be judged by driving,
    and a scp-unzip-restart cycle per guess is the slow way to find them."""
    limits = {"motor_min": (0.0, 0.6), "turn_scale": (0.05, 1.0),
              "trim_left": (0.5, 1.0), "trim_right": (0.5, 1.0),
              "balance_p": (0.0, 1.0), "balance_i": (0.0, 1.5),
              "balance_smooth": (0.0, 0.95), "balance_seed": (-0.3, 0.3),
              "balance_trim": (-0.25, 0.25)}
    global MOTOR_MIN, TURN_SCALE, TRIM_LEFT, TRIM_RIGHT, BALANCE_ENABLED, _balance_bias
    global BALANCE_P, BALANCE_GAIN, BALANCE_SMOOTH, BALANCE_SEED, _balance_i, _err_smooth
    global BALANCE_TRIM
    changed = {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if data.get("reset"):
            MOTOR_MIN  = _TUNE_DEFAULTS["motor_min"]
            TURN_SCALE = _TUNE_DEFAULTS["turn_scale"]
            TRIM_LEFT  = _TUNE_DEFAULTS["trim_left"]
            TRIM_RIGHT = _TUNE_DEFAULTS["trim_right"]
            BALANCE_ENABLED = _TUNE_DEFAULTS["balance_enabled"]
            BALANCE_P = _TUNE_DEFAULTS["balance_p"]
            BALANCE_GAIN = _TUNE_DEFAULTS["balance_i"]
            BALANCE_SMOOTH = _TUNE_DEFAULTS["balance_smooth"]
            BALANCE_SEED = _TUNE_DEFAULTS["balance_seed"]
            BALANCE_TRIM = _TUNE_DEFAULTS["balance_trim"]
            _balance_bias = _balance_i = _err_smooth = 0.0
            global _enc_swap
            _enc_swap = False
            try:
                os.remove(TUNE_FILE)
            except FileNotFoundError:
                pass
            return jsonify(dict(_TUNE_DEFAULTS, reset=True,
                                note="back to the values in robot.py"))
        for key, (lo, hi) in limits.items():
            if key in data:
                try:
                    v = float(data[key])
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} must be a number"}), 400
                if not lo <= v <= hi:
                    return jsonify({"error": f"{key} must be between {lo} and {hi}"}), 400
                changed[key] = v
        if "balance_enabled" in data:
            changed["balance_enabled"] = bool(data["balance_enabled"])

        if "motor_min" in changed:   MOTOR_MIN  = changed["motor_min"]
        if "turn_scale" in changed:  TURN_SCALE = changed["turn_scale"]
        if "trim_left" in changed:   TRIM_LEFT  = changed["trim_left"]
        if "trim_right" in changed:  TRIM_RIGHT = changed["trim_right"]
        if "balance_p" in changed:      BALANCE_P = changed["balance_p"]
        if "balance_i" in changed:      BALANCE_GAIN = changed["balance_i"]
        if "balance_smooth" in changed: BALANCE_SMOOTH = changed["balance_smooth"]
        if "balance_seed" in changed:   BALANCE_SEED = changed["balance_seed"]
        if "balance_trim" in changed:   BALANCE_TRIM = changed["balance_trim"]
        if any(k.startswith("balance_") for k in changed):
            _balance_i = _err_smooth = _balance_bias = 0.0   # retune from zero
        if "balance_enabled" in changed:
            BALANCE_ENABLED = changed["balance_enabled"]
            _balance_bias = 0.0      # a stale correction must not carry over
            global _bal_fault, _bal_fault_kind
            _bal_fault = _bal_fault_kind = None   # switching it on means "try again"
            _clear_checks()

        if changed:
            try:
                save_tune()
            except Exception as exc:
                return jsonify({"error": f"could not save: {exc}"}), 500

    return jsonify({"motor_min": MOTOR_MIN, "turn_scale": TURN_SCALE,
                    "trim_left": TRIM_LEFT, "trim_right": TRIM_RIGHT,
                    "balance_enabled": BALANCE_ENABLED,
                    "balance_p": BALANCE_P, "balance_i": BALANCE_GAIN,
                    "balance_smooth": BALANCE_SMOOTH, "balance_seed": BALANCE_SEED,
                    "balance_trim": BALANCE_TRIM, "swap_encoders": _enc_swap,
                    "changed": changed,
                    "persists": True,
                    "saved_to": TUNE_FILE,
                    "defaults": _TUNE_DEFAULTS,
                    "note": "saved; survives restart and redeploy. "
                            "POST {\"reset\": true} to go back to robot.py values"})

@app.route("/battery")
def battery():
    """Return battery voltage reading.

    When the ADC is missing this reports the underlying reason rather than a
    bare null, so the failure can be diagnosed from the phone."""
    if not ADC_AVAILABLE:
        return jsonify({"voltage": None, "percent": None, "error": ADC_ERROR})
    if ADC_AVAILABLE:
        try:
            # Carrier Rev F/G: A0 is on a 10k/10k divider, so the pin sees
            # half the pack. (The hand-wired build had no divider and used
            # x1 - that is why this was 1.0 until 2026-11-05b.)
            voltage = battery_channel.voltage * BATTERY_SCALE
            percent = int(((voltage - 3.0) / (4.2 - 3.0)) * 100)
            percent = max(0, min(100, percent))
            return jsonify({"voltage": round(voltage, 2), "percent": percent,
                            "low": batt_low,
                            "rest_voltage": None if batt_idle_v is None else round(batt_idle_v, 2)})
        except Exception as e:
            return jsonify({"voltage": None, "percent": None, "error": str(e)})
    return jsonify({"voltage": None, "percent": None})

@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Safely shut down the Pi"""
    if GPIO_AVAILABLE:
        motor_left.stop()
        motor_right.stop()
        headlight.off()
        for _led in (rgb_leds or []):
            _led.off()
    camera.stop()
    ok = _power_off("shutdown button")
    return jsonify({"status": "shutting down" if ok else
                    "shutdown not permitted - run the one-time sudoers command"})

def _power_off(reason):
    """Halt the Pi. Needs the one-time sudoers rule (see BATTERY GUARD):
    the service runs as your user with no terminal, so plain sudo can't ask for
    a password and silently fails - which is why the shutdown button never
    actually worked before 2026-11-11a. -n makes sudo fail fast instead."""
    print(f"[POWER] Shutting down: {reason}")
    r = subprocess.run(["sudo", "-n", "/usr/sbin/shutdown", "-h", "now"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[POWER] shutdown refused: {r.stderr.strip()} "
              "- install /etc/sudoers.d/zerobot-shutdown")
        return False
    return True

# ─── BATTERY GUARD (low-battery cutoff) ───────────────────────────────────────
# Shuts the Pi down cleanly before the pack runs flat, so the SD card isn't
# corrupted by a brownout and the protected cells aren't drained to cutoff.
# Readings taken under load (driving, talking) sag, so only IDLE readings
# count, and only after the pack has had a few seconds to recover. Needs
# 30 s of consecutive idle readings at or below the cutoff before acting.
BATT_WARN_V = 3.45          # UI shows LOW; about 10% left at rest
BATT_CUTOFF_V = 3.30        # clean shutdown; a few % left at rest, safe margin
BATT_CHECK_S = 5
BATT_SAMPLES = 6            # 6 x 5 s = 30 s of agreement before shutting down
BATT_RECOVER_S = 10         # ignore readings this long after any load
BATT_INVALID_V = 2.5        # below this the ADC is misreading, not the pack

batt_low = False
batt_idle_v = None          # median of recent idle readings
_batt_samples = collections.deque(maxlen=BATT_SAMPLES)

def read_pack_voltage():
    if not ADC_AVAILABLE:
        return None
    try:
        return battery_channel.voltage * BATTERY_SCALE
    except Exception:
        return None

def battery_guard():
    global batt_low, batt_idle_v
    last_load = time.monotonic()
    while True:
        time.sleep(BATT_CHECK_S)
        try:
            loaded = motors_active or (voice is not None and voice.busy)
            now = time.monotonic()
            if loaded:
                last_load = now
                continue
            if now - last_load < BATT_RECOVER_S:
                continue
            v = read_pack_voltage()
            if v is None or v < BATT_INVALID_V:
                continue
            _batt_samples.append(v)
            batt_idle_v = sorted(_batt_samples)[len(_batt_samples) // 2]
            was_low = batt_low
            batt_low = batt_idle_v <= BATT_WARN_V
            if batt_low and not was_low:
                print(f"[POWER] Battery low: {batt_idle_v:.2f} V at rest")
            if (len(_batt_samples) == BATT_SAMPLES
                    and max(_batt_samples) <= BATT_CUTOFF_V):
                print(f"[POWER] Battery at cutoff ({max(_batt_samples):.2f} V) - stopping everything")
                if voice is not None:
                    voice.stop()
                if GPIO_AVAILABLE:
                    motor_left.stop()
                    motor_right.stop()
                    headlight.off()
                    for _led in (rgb_leds or []):
                        _led.off()
                try:
                    camera.stop()
                except Exception:
                    pass
                if _power_off(f"battery {max(_batt_samples):.2f} V"):
                    return
                _batt_samples.clear()      # not permitted: try again in 30 s
        except Exception as e:
            print(f"[POWER] battery guard error: {e}")

if ADC_AVAILABLE:
    threading.Thread(target=battery_guard, daemon=True).start()
    print(f"[OK] Battery guard: warn {BATT_WARN_V} V, shut down at {BATT_CUTOFF_V} V (at rest)")
else:
    print("[WARN] No battery reading - low-battery cutoff is OFF")

# ─── STARTUP ──────────────────────────────────────────────────────────────────
def cleanup(sig=None, frame=None):
    """Clean up hardware on exit"""
    print("\n[INFO] Shutting down...")
    if voice is not None:
        voice.stop()
    if GPIO_AVAILABLE:
        motor_left.stop()
        motor_right.stop()
        headlight.off()
        for _led in (rgb_leds or []):
            _led.off()
    camera.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

def get_ip():
    """Find this Pi's LAN IP. Opens a throwaway UDP socket and asks the OS
    which interface it would use — no packets are actually sent."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()

TLS_DIR = os.path.expanduser("~/.zerobot_tls")   # outside ~/zerobot: survives redeploys,
HTTPS_PORT = 5443                                  # so the phone's "proceed anyway" sticks

def start_https(ip):
    """Second copy of the same app on HTTPS, for the TALK page's microphone.
    The certificate is self-signed (setup.sh makes it), so the phone warns
    once - tap Advanced -> Proceed. Port 5000 stays plain HTTP and unchanged:
    the video on :8080 is HTTP, and an HTTPS page can't show it."""
    cert = os.path.join(TLS_DIR, "cert.pem")
    key = os.path.join(TLS_DIR, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        print(f"[WARN] No TLS certificate in {TLS_DIR} - TALK page unavailable "
              "(setup.sh makes one; restart the service)")
        return
    try:
        from werkzeug.serving import make_server
        srv = make_server("0.0.0.0", HTTPS_PORT, app, threaded=True,
                          ssl_context=(cert, key))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[INFO] Talk page:         https://{ip}:{HTTPS_PORT}/talk")
    except Exception as e:
        print(f"[WARN] HTTPS server failed ({e}) - TALK page unavailable")

if __name__ == "__main__":
    ip = get_ip()
    print(f"[INFO] ZeroBot {STAMP}  (pid {os.getpid()})")
    load_tune()
    print("[INFO] Robot server starting on port 5000")
    print(f"[INFO] Control interface: http://{ip}:5000")
    # hflip/vflip: the camera is mounted inverted in the face plate.
    # 640x480 is a native binned sensor mode on the OV5647 - the startup log
    # confirms libcamera selecting it directly. Asking for anything else makes
    # the ISP rescale every frame, which ADDS latency. Keep this size.
    #
    # fps is the real latency lever: at 15fps a frame can sit up to 66ms just
    # waiting its turn. 25fps roughly halves that, and the JPEG encode is in
    # hardware so it costs bandwidth rather than CPU.
    # No geometry here on purpose. camera.py detects the sensor and picks the
    # profile: OV5647 kit builds get native 640x480, CM3 builds get full-FOV
    # 2304x1296 downscaled to 1024x576 with focus fixed. One zip, both robots.
    camera.start(port=8080, hflip=1, vflip=1)
    print(f"[INFO] Camera stream:     http://{ip}:8080/stream")
    start_https(ip)
    print("[INFO] Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
