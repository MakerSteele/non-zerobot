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
        if "busy" in str(e).lower():
            print("[WARN]   13l: the googlevoicehat mic overlay holds GPIO 16 - "
                  "sudo bash ~/zerobot/setup-i2s-mic.sh && sudo reboot")

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
# Gentle first boot (12 Nov 2026): until the Setup check has passed once on this
# robot, start at 40% so a first test on the bench can't launch it off the table.
# The flag file lives outside ~/zerobot, so reinstalls keep it.
SETUP_OK_FILE = os.path.expanduser("~/.zerobot_setup_ok")
FIRST_BOOT = not os.path.exists(SETUP_OK_FILE)
current_speed = 0.4 if FIRST_BOOT else 0.7   # default 70% once setup has passed

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
# 13q: a link that adds internet lag (the optional remote add-on) can ask for a
# longer cutoff; each /move sets the timeout for its own link (link_timeout()).
_cmd_timeout = WATCHDOG_TIMEOUT
last_command_time = time.monotonic()
motors_active = False

def watchdog():
    global motors_active
    while True:
        time.sleep(0.1)
        if motors_active and (time.monotonic() - last_command_time) > _cmd_timeout:
            _v = globals().get("voice")
            if _v is not None:
                _v.set_drive(0.0, 0.0)      # 13b: also stops driving-while-talking
            if GPIO_AVAILABLE and not (_v is not None and _v.busy):
                motor_left.stop()           # never touch the pins mid-speech
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

# ─── MIC (robot -> phone, 13f) ───────────────────────────────────────────────
# 13k: SPH0645 I2S mic (preferred) or MAX4466 -> XIAO SAMD21 -> UART (fallback).
# MAX4466 mic -> XIAO SAMD21 ADC -> UART 1 Mbaud -> /dev/serial0. mic_live.py
# cleans it (same filters as mic_pi_test.py v9) and streams 16 kHz PCM to the
# LISTEN button on the talk page. The port is only open while someone listens.
# Muted while the robot talks through its motors, so it never hears itself.
# 13j: ONE build for every robot. At start-up (and every 30 s until found)
# mic_live.probe() listens on the UART for a XIAO; no mic = LISTEN hidden.
# Force with ~/.zerobot_mic containing "on" or "off".
mic = None
try:
    import mic_live as _mic_mod
    mic = _mic_mod.MicLive(is_muted=lambda: voice is not None and voice.busy)
    mic.present = False
    mic.reason = "checking for the mic"
    if voice is not None:
        voice.before_audio = mic.pause_for_voice    # 13k: I2S mic hands the PCM block back
    # 13k: if the service died while the I2S mic had pigpio's PCM settings
    # cleared, the voice would hang - put them back now (no-op otherwise).
    if os.path.exists("/run/zerobot-pcm.json"):
        _ok, _msg = _mic_mod._pcm("restore")
        print(f"[{'OK' if _ok else 'WARN'}] Mic: leftover pigpio hand-over restored ({_msg})", flush=True)

    def _mic_probe_loop():
        time.sleep(3)                       # let the XIAO finish booting
        while True:
            if not mic.running:
                ok, why, src = _mic_mod.probe()
                mic.present, mic.reason = ok, why
                if src:
                    mic.source = src
                print(f"[{'OK' if ok else 'INFO'}] Mic: {why}", flush=True)
                if ok or "Pi Zero W" in why or "~/.zerobot_mic" in why or "not the main UART" in why \
                        or "console" in why:
                    return                  # found, or this robot can't have one: stop looking
            time.sleep(30)
    threading.Thread(target=_mic_probe_loop, daemon=True).start()
except Exception as e:
    mic = None
    print(f"[WARN] Mic unavailable ({e})")

def _voice_drive(left, right):
    """13b: give voice.py the joystick drive, so the robot keeps moving while
    it talks. voice.py works in RAW driver channels (A = GPIO 17/27, B =
    22/23, + = the first pin), so undo SWAP_MOTORS and INVERT_A/B here - the
    same mapping gpiozero's Motor objects use. Same 0.05 dead zone as /move."""
    if voice is None:
        return
    left = 0.0 if abs(left) <= 0.05 else left
    right = 0.0 if abs(right) <= 0.05 else right
    a, b = (right, left) if SWAP_MOTORS else (left, right)
    voice.set_drive(-a if INVERT_A else a, -b if INVERT_B else b)


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

# ─── OPTIONAL ADD-ONS (13q) ───────────────────────────────────────────────────
# Personal extras live in ~/zerobot_addons, OUTSIDE ~/zerobot: deploys don't
# touch them, and kit robots simply don't have the folder. Each add-on is a
# module with attach(app) / start() / stop(). Missing folder = feature absent.
ADDON_DIR = os.path.expanduser("~/zerobot_addons")
if os.path.isdir(ADDON_DIR) and ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)
try:
    import remote as remote_addon          # optional remote-access add-on
    remote_addon.attach(app)
    print(f"[OK] Add-on: remote access ({remote_addon.__file__})")
except ImportError:
    remote_addon = None
except Exception as e:
    remote_addon = None
    print(f"[WARN] Remote add-on failed to load ({e}) - carrying on without it")

# 20a: further add-ons, loaded AFTER remote (they may build on it). Named here
# on purpose - nothing else in ~/zerobot_addons is ever imported. Each is a
# module with attach(app), optional start()/stop(), and optional
# drive_filter(request, x, y) -> (x, y), which /move applies to the joystick.
EXTRA_ADDONS = ("guest",)                  # optional add-on, not part of this repo
extra_addons = []
if os.path.isdir(ADDON_DIR):
    import importlib
    for _name in EXTRA_ADDONS:
        if not os.path.isfile(os.path.join(ADDON_DIR, _name + ".py")):
            continue
        try:
            _mod = importlib.import_module(_name)
            _mod.attach(app)
            extra_addons.append(_mod)
            print(f"[OK] Add-on: {_name} ({_mod.__file__})")
        except Exception as e:
            print(f"[WARN] Add-on {_name} failed to load ({e}) - carrying on without it")

def is_remote():
    """True when this request came over the remote add-on's internet link."""
    return remote_addon is not None and remote_addon.is_remote(request)

def link_timeout():
    return remote_addon.WATCHDOG_TIMEOUT if is_remote() else WATCHDOG_TIMEOUT

@app.route("/cam/frame")
def cam_frame():
    """One JPEG, the next one the camera makes. The remote page asks for a
    frame only after the last one arrived, so at most one frame is ever in
    flight and the picture can't fall seconds behind on a slow link."""
    out = camera._output
    with out.condition:
        out.condition.wait(timeout=1.0)
        frame = out.frame
    if frame is None or out.age() > 3:
        return jsonify({"ok": False, "message": "no recent frame"}), 503
    return Response(frame, content_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})

@app.route("/recordings")
@app.route("/recordings/<path:name>")
def recordings_relay(name=None):
    """The recordings page (camera.py, :8080) for HTTPS and remote pages. Its
    links are /recordings/..., so serving it at the same path keeps them working.
    Range is passed through so videos can seek."""
    import http.client
    path = "/recordings" + ("/" + name if name else "")
    qs = request.query_string.decode("latin-1")
    hdrs = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=15)
    try:
        conn.request("GET", path + ("?" + qs if qs else ""), headers=hdrs)
        r = conn.getresponse()
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"camera not answering ({e})"}), 502
    keep = {k: v for k, v in r.getheaders()
            if k.lower() in ("content-type", "content-length", "content-range",
                             "accept-ranges", "location", "content-disposition")}
    def body():
        try:
            while True:
                b = r.read(65536)
                if not b:
                    break
                yield b
        finally:
            conn.close()
    return Response(body(), status=r.status, headers=keep, direct_passthrough=True)


@app.route("/video")
def video():
    """Old URL kept for compatibility — points at the real stream."""
    host = request.host.split(":")[0]
    return (f'<html><body style="margin:0"><img src="http://{host}:8080/stream" '
            f'style="width:100%"></body></html>')

# 13c: camera relay for the HTTPS drive page. An HTTPS page can't load the
# plain-HTTP camera on :8080, so the drive page over :5443 (needed for the
# push-to-talk mic) fetches /cam/... from here instead. Only GETs of the
# camera's own read-only paths are relayed. The plain-HTTP page doesn't use
# this at all - it still talks to :8080 directly, exactly as before.
_CAM_PATHS = ("stream", "health", "record/status", "record/start", "record/stop")

@app.route("/cam/<path:sub>")
def cam_relay(sub):
    import http.client
    if sub not in _CAM_PATHS:
        return jsonify({"ok": False, "message": "not relayed"}), 404
    qs = request.query_string.decode("latin-1")
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
    try:
        conn.request("GET", "/" + sub + ("?" + qs if qs else ""))
        r = conn.getresponse()
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"camera not answering ({e})"}), 502
    ctype = r.getheader("Content-Type", "application/octet-stream")
    if sub != "stream":
        try:
            body = r.read()
        finally:
            conn.close()
        return Response(body, status=r.status, content_type=ctype,
                        headers={"Cache-Control": "no-store"})

    def relay():
        try:
            while True:
                b = r.read1(65536)      # whatever has arrived - no waiting for a full buffer
                if not b:
                    break
                yield b
        finally:
            conn.close()
    return Response(relay(), status=r.status, content_type=ctype, direct_passthrough=True,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

@app.route("/")
def index():
    """Serve the main control page.

    The stamp is baked into the HTML at render time, so a page the phone has
    cached carries the OLD stamp while /version returns the live one. That
    disagreement is what tells you to hard-refresh instead of hunting a
    phantom bug in code the browser never loaded."""
    return render_template("index.html", version=STAMP, remote=is_remote())

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
    # 13b: with DRIVE WHILE TALKING on (talk page), the joystick drive is mixed
    # into the voice waveform instead; off = driving locked, as before.
    talking = voice is not None and voice.busy
    if talking and not voice.drive_on:
        return jsonify({"status": "speaking", "left": 0.0, "right": 0.0})
    data = request.get_json()
    x = float(data.get("x", 0))  # Left/right  (-1 = full left, 1 = full right)
    y = float(data.get("y", 0))  # Fwd/back    (-1 = full back, 1 = full forward)
    # 20a: add-ons may limit the joystick (Guest Drive's guest speed limit).
    # A filter that breaks stops the robot rather than letting it run unlimited.
    for _a in extra_addons:
        _f = getattr(_a, "drive_filter", None)
        if _f is not None:
            try:
                x, y = _f(request, x, y)
            except Exception as e:
                print(f"[WARN] drive filter {_a.__name__} failed ({e}) - stopping", flush=True)
                x = y = 0.0

    global _cmd_timeout
    _cmd_timeout = link_timeout()
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

    _voice_drive(left_speed, right_speed)   # 13b: ready if speech starts mid-drive
    if talking:
        motors_active = (abs(left_speed) > 0.05 or abs(right_speed) > 0.05)
        return jsonify({"status": "speaking", "left": left_speed, "right": right_speed})

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
    _voice_drive(0.0, 0.0)                  # 13b
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
    if "drive" in data:                              # 13b: DRIVE WHILE TALKING switch
        voice.set_drive_enabled(bool(data["drive"]))
        if not voice.drive_on:
            voice.set_drive(0.0, 0.0)
    if "fixed62" in data or "dither" in data:       # 11m tone-test switches
        voice.set_tone_test(fixed62=data.get("fixed62"), dither=data.get("dither"))
    return jsonify({"level": voice.level, "smooth": voice.smooth, "power": voice.power,
                    "clarity": voice.clarity, "fixed62": voice.fixed62, "dither": voice.use_dither,
                    "drive": voice.drive_on})

@app.route("/voice/say", methods=["POST"])
def voice_say():
    if voice is None:
        return _no_voice()
    data = request.get_json(silent=True) or {}
    ok, msg = voice.say(data.get("text", ""), data.get("voice"))
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

# ─── MIC ROUTES (13f) ─────────────────────────────────────────────────────────
@app.route("/mic", methods=["GET"])
def mic_status():
    if mic is None:
        return jsonify({"available": False})
    return jsonify(mic.status())

@app.route("/mic/stream")
def mic_stream():
    """Endless raw 16 kHz mono s16le. The phone reads it with fetch() and plays
    it through Web Audio. Closing the fetch on the phone unsubscribes."""
    if mic is None:
        return jsonify({"available": False, "error": "mic_live.py did not load"}), 503
    if not mic.present:
        return jsonify({"available": False, "error": mic.reason}), 503
    import queue as _q
    q = mic.subscribe()

    def gen():
        try:
            while True:
                try:
                    yield q.get(timeout=2.0)
                except _q.Empty:
                    if not mic.running:
                        return                  # reader died (see /mic error)
                    yield bytes(1024)           # 32 ms of silence: checks the phone is still there
        finally:
            mic.unsubscribe(q)

    return Response(gen(), mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
                    direct_passthrough=True)

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

@app.route("/speed", methods=["GET", "POST"])
def set_speed():
    """Set motor speed multiplier (0.0 to 1.0). GET reports it, so the page's
    SPD slider can start where the robot really is (40% on a first boot)."""
    global current_speed
    if request.method == "GET":
        return jsonify({"speed": current_speed, "first_boot": FIRST_BOOT})
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

# ─── POWER LOG (13j) ──────────────────────────────────────────────────────────
# 13 Nov: the robot browned out (log just stopped) while recording + driving +
# talking + listening. This writes one line every 5 s to ~/zerobot_power.log,
# forced to the SD card each time so the lines before a power cut survive:
# pack volts (min/max of 0.5 s samples - catches the sag under load), the Pi's
# power flags, temperature, and what was running. After a crash:
#     tail -40 ~/zerobot_power.log
# Flags (get_throttled): bit 0 = under-voltage NOW, bit 2 = throttled NOW,
# bit 16/18 = has happened since boot. 0x0 = power has been fine.
POWER_LOG = os.path.expanduser("~/zerobot_power.log")
POWER_LOG_S = 5
POWER_LOG_MAX = 2_000_000                  # bytes, then the old file becomes .1

def _throttled():
    try:
        with open("/sys/devices/platform/soc/soc:firmware/get_throttled") as f:
            return int(f.read().strip(), 16)
    except Exception:
        pass
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None

def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000.0
    except Exception:
        return None

def _power_line(vs):
    v = f"{vs[-1]:.2f} V (min {min(vs):.2f} max {max(vs):.2f})" if vs else "no ADC"
    thr = _throttled()
    t = _cpu_temp()
    try:
        rec = bool(camera.recorder._running)
    except Exception:
        rec = None
    vbusy = voice is not None and voice.busy
    bits = [time.strftime("%Y-%m-%d %H:%M:%S"), v,
            "flags " + (f"0x{thr:x}" if thr is not None else "?"),
            f"{t:.0f}C" if t is not None else "?C",
            "|", "DRIVE" if motors_active else "idle",
            "REC" if rec else "", ("VOICE:" + str(voice.mode)) if vbusy else "",
            f"MIC x{len(mic.subs)}" if (mic is not None and mic.subs) else "",
            "load %.1f" % os.getloadavg()[0]]
    return " ".join(b for b in bits if b), thr

def power_log():
    last_now = None
    try:
        with open(POWER_LOG, "a") as f:
            f.write(f"===== start {time.strftime('%Y-%m-%d %H:%M:%S')} build {STAMP}\n")
            f.flush(); os.fsync(f.fileno())
    except Exception as e:
        print(f"[WARN] Power log unavailable ({e})")
        return
    while True:
        vs = []
        for _ in range(int(POWER_LOG_S / 0.5)):
            v = read_pack_voltage()
            if v is not None and v >= BATT_INVALID_V:
                vs.append(v)
            time.sleep(0.5)
        try:
            line, thr = _power_line(vs)
            if os.path.exists(POWER_LOG) and os.path.getsize(POWER_LOG) > POWER_LOG_MAX:
                os.replace(POWER_LOG, POWER_LOG + ".1")
            with open(POWER_LOG, "a") as f:
                f.write(line + "\n")
                f.flush(); os.fsync(f.fileno())
            now_bits = (thr & 0xF) if thr is not None else None
            if now_bits != last_now:          # the journal only gets CHANGES
                if now_bits:
                    print(f"[POWER] LOW POWER NOW: {line}", flush=True)
                elif last_now:
                    print(f"[POWER] power back to normal: {line}", flush=True)
                last_now = now_bits
        except Exception as e:
            print(f"[POWER] power log error: {e}", flush=True)

threading.Thread(target=power_log, daemon=True).start()
print(f"[OK] Power log: {POWER_LOG} every {POWER_LOG_S} s")

# ─── STARTUP ──────────────────────────────────────────────────────────────────
def cleanup(sig=None, frame=None):
    """Clean up hardware on exit"""
    print("\n[INFO] Shutting down...")
    if remote_addon is not None:
        try:
            remote_addon.stop()             # let the add-on shut down cleanly
        except Exception:
            pass
    for _a in extra_addons:
        try:
            if hasattr(_a, "stop"):
                _a.stop()
        except Exception:
            pass
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

# ─── SETUP CHECK (12 Nov 2026) ───────────────────────────────────────────────
# One red/green list of everything a builder can get wrong, in plain words.
# The page is /setup; the data is /setupcheck. Each item: state is pass, warn,
# fail or wait (needs the robot to do something first), with a "fix" line that
# matches ASSEMBLY.md's Fix-it table. Read-only: it never moves the motors.

def _sh(cmd, timeout=3):
    """Run a short command, return (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)

def _robot_processes():
    """How many robot.py processes are running (should be exactly 1)."""
    n = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
        except Exception:
            continue
        if b"python" in cmd and b"robot.py" in cmd:
            n += 1
    return n

def _item(id_, label, state, detail="", fix=""):
    return {"id": id_, "label": label, "state": state, "detail": detail, "fix": fix}

def setup_items():
    items = []

    # 1. Software: one copy running
    n = _robot_processes()
    if n <= 1:
        items.append(_item("software", "Robot software", "pass", f"Build {STAMP}"))
    else:
        items.append(_item("software", "Robot software", "fail",
                           f"{n} copies of robot.py are running",
                           "Restart it: sudo systemctl restart zerobot (or reboot)."))

    # 2. Battery sensor (ADS1115) answering
    if ADC_AVAILABLE:
        items.append(_item("adc", "Battery sensor (ADS1115)", "pass", "Found on I2C"))
    else:
        items.append(_item("adc", "Battery sensor (ADS1115)", "fail", str(ADC_ERROR),
                           "Check it's seated with VDD at the VDD mark, and the Pi link wires on "
                           "pins 1, 3 and 5. On a terminal, i2cdetect -y 1 should show 48."))

    # 3. Battery voltage (1S Li-ion: 3.0-4.2 V)
    if ADC_AVAILABLE:
        try:
            v = battery_channel.voltage * BATTERY_SCALE
            if v > 4.35:
                items.append(_item("battery", "Battery voltage", "fail", f"{v:.2f} V",
                                   "Too high for one Li-ion cell. Switch off NOW: this robot is "
                                   "1S (3.7 V) only. A 2-cell pack damages the boost and charger."))
            elif v >= 3.4:
                items.append(_item("battery", "Battery voltage", "pass", f"{v:.2f} V"))
            elif v >= 3.0:
                items.append(_item("battery", "Battery voltage", "warn", f"{v:.2f} V",
                                   "Getting low. Charge it soon."))
            else:
                items.append(_item("battery", "Battery voltage", "fail", f"{v:.2f} V",
                                   "Below 3.0 V. Charge the battery. If it stays near 0 V, a cell's "
                                   "protection has tripped: find the short before charging."))
        except Exception as e:
            items.append(_item("battery", "Battery voltage", "fail", str(e),
                               "The sensor stopped answering. Check its seating and wires."))
    else:
        items.append(_item("battery", "Battery voltage", "wait", "Needs the battery sensor first"))

    # 4. Pi power supply (under-voltage flags)
    rc, out = _sh(["vcgencmd", "get_throttled"])
    if rc == 0 and "=" in out:
        try:
            flags = int(out.split("=")[1], 16)
        except ValueError:
            flags = None
        if flags is None:
            items.append(_item("power", "Pi power supply", "warn", out))
        elif flags & 0x1:
            items.append(_item("power", "Pi power supply", "fail", "Low voltage right now",
                               "The Pi isn't getting enough power. Check the charger board's 5 V "
                               "(4.9-5.25 V), charge the battery, and unplug the USB-C charger while testing."))
        elif flags & 0x50000:
            items.append(_item("power", "Pi power supply", "warn", "Low voltage happened since boot",
                               "Usually a weak or nearly flat battery, or talking very loudly. "
                               "Charge it and keep an eye on this."))
        else:
            items.append(_item("power", "Pi power supply", "pass", "No low-voltage events"))
    else:
        items.append(_item("power", "Pi power supply", "warn", "vcgencmd not available"))

    # 5. Camera
    try:
        model = camera._cfg.get("_sensor")
        age = camera._output.age()
    except Exception:
        model, age = None, None
    if model and age is not None and age < 3:
        items.append(_item("camera", "Camera", "pass", f"{model}, streaming"))
    elif model:
        items.append(_item("camera", "Camera", "warn", f"{model} found, no recent frames",
                           "If the picture stays black, power off and reseat the ribbon. "
                           "A camera that's found but gives no picture usually means a damaged ribbon."))
    else:
        items.append(_item("camera", "Camera", "fail", "No camera found",
                           "Power off, then check the ribbon at both ends: contacts the right way, "
                           "latches closed. Pi Zero boards need the 15-to-22 ribbon."))

    # 6. Wheel encoders and pairing
    if not ENCODERS_AVAILABLE:
        items.append(_item("encoders", "Wheel speed sensors", "fail", "Not available",
                           "The pigpio service may not be running: sudo systemctl status pigpiod. "
                           "Then check both motor plugs are fully home."))
    elif _bal_fault is not None:
        items.append(_item("encoders", "Wheel speed sensors", "fail", str(_bal_fault),
                           "Tap BAL on the control page to see why and retry. "
                           "A silent sensor usually means a loose motor plug."))
    elif _pair_state == "confirmed":
        items.append(_item("encoders", "Wheel speed sensors", "pass",
                           f"Both counting and on the right motors ({_enc_left_total} / {_enc_right_total} pulses)"))
    elif _enc_left_total == 0 and _enc_right_total == 0:
        items.append(_item("encoders", "Wheel speed sensors", "wait", "No pulses yet",
                           "Drive forward a little, or run the pair check below (wheels off the table)."))
    else:
        items.append(_item("encoders", "Wheel speed sensors", "wait",
                           f"Counting ({_enc_left_total} / {_enc_right_total} pulses), pairing not confirmed yet",
                           "Drive in a few turns, or run the pair check below (wheels off the table)."))

    # 7. Power-off permission (low-battery cutoff and the OFF button)
    rc, _ = _sh(["sudo", "-n", "-l", "/usr/sbin/shutdown"])
    if rc == 0:
        items.append(_item("shutdown", "Safe power-off", "pass", "Allowed"))
    else:
        items.append(_item("shutdown", "Safe power-off", "warn", "Not allowed yet",
                           "Low-battery cutoff and the OFF button won't work. Re-run the installer."))

    # 8. Talk page certificate
    tls = os.path.expanduser("~/.zerobot_tls")
    if os.path.exists(os.path.join(tls, "cert.pem")):
        items.append(_item("talk", "Talk page", "pass", "Certificate ready"))
    else:
        items.append(_item("talk", "Talk page", "warn", "No certificate yet",
                           "Restart the robot once; the setup makes one."))
    return items

@app.route("/setup")
def setup_page():
    return render_template("setup.html", version=STAMP)

@app.route("/setupcheck")
def setupcheck():
    items = setup_items()
    ok = not any(i["state"] in ("fail", "wait") for i in items)
    if ok and FIRST_BOOT and not os.path.exists(SETUP_OK_FILE):
        try:
            with open(SETUP_OK_FILE, "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + STAMP + "\n")
            print("[OK] Setup check passed - normal 70% start speed from the next boot")
        except Exception as e:
            print(f"[WARN] Could not save the setup-passed flag ({e})")
    return jsonify({"ok": ok, "items": items, "first_boot": FIRST_BOOT,
                    "setup_ok_saved": os.path.exists(SETUP_OK_FILE), "stamp": STAMP})

TLS_DIR = os.path.expanduser("~/.zerobot_tls")   # outside ~/zerobot: survives redeploys,
HTTPS_PORT = 5443                                  # so the phone's "proceed anyway" sticks

# ─── START-UP CHIME (13 Nov 2026) ────────────────────────────────────────────
# Rising three notes through the motors once the robot is on the network and
# its pages are up = "ready, connect now". If no WiFi turns up within
# CHIME_WAIT_S, two falling notes instead = "running, but no network".
# Plays on every start of the service (boot, deploy, watchdog restart).
CHIME_ON_START = True
CHIME_WAIT_S = 90

def startup_chime():
    if voice is None or not CHIME_ON_START:
        return
    try:
        t0 = time.monotonic()
        ip = get_ip()
        while (ip == "0.0.0.0" or ip.startswith("127.")) and time.monotonic() - t0 < CHIME_WAIT_S:
            time.sleep(2)
            ip = get_ip()
        online = not (ip == "0.0.0.0" or ip.startswith("127."))
        time.sleep(2)                       # let Flask and the camera finish starting
        for _ in range(10):                 # someone may already be talking/driving
            if not voice.busy:
                break
            time.sleep(1)
        ok, msg = voice.chime(online)
        print(f"[INFO] Start-up chime ({'online ' + ip if online else 'NO network'}): "
              f"{'played' if ok else msg}", flush=True)
    except Exception as e:
        print(f"[WARN] Start-up chime failed ({e})", flush=True)

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
    if remote_addon is not None:
        remote_addon.start()
    for _a in extra_addons:
        try:
            if hasattr(_a, "start"):
                _a.start()
        except Exception as e:
            print(f"[WARN] Add-on {_a.__name__} didn't start ({e})")
    threading.Thread(target=startup_chime, daemon=True).start()
    print("[INFO] Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
