#!/usr/bin/env python3
"""
ZeroBot camera server
=====================
Serves the camera as an MJPEG stream on its own port (8080) — the same
architecture the original ZeroBot used with mjpg-streamer:

    browser <img> --HTTP--> this server <-- hardware JPEG encoder <-- sensor

Flask never sees a frame. The only per-frame work Python does is hand the
encoder's finished JPEG buffer to the network socket. That is what makes
the stream viable on a single-core Pi Zero W.

Based on the official Picamera2 MJPEG streaming example.
"""

import io
import json
import os
import socket
import socketserver
from http import server
import time
from threading import Condition, Lock, Thread

from version import STAMP
import recorder as _recorder_mod
from recorder import recorder, serve_http as _recording_http

try:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    from libcamera import Transform
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

# AfModeEnum.Manual is 0 in every libcamera that has it. Import the symbol
# when available and fall back to the literal, so this file still loads on
# a build whose libcamera predates the autofocus controls.
try:
    from libcamera import controls as _lc_controls
    _AF_MANUAL = _lc_controls.AfModeEnum.Manual
except Exception:
    _AF_MANUAL = 0

# Same idea for the one-shot autofocus path. Auto is 1 and Start is 0 in every
# libcamera that defines them; the literals keep this file importable on a
# build that predates the controls, where focus_available() returns False
# anyway and neither value is ever used.
try:
    _AF_AUTO = _lc_controls.AfModeEnum.Auto
    _AF_TRIGGER_START = _lc_controls.AfTriggerEnum.Start
except Exception:
    _AF_AUTO = 1
    _AF_TRIGGER_START = 0


class StreamingOutput(io.BufferedIOBase):
    """The encoder writes each finished JPEG here; client threads wait on
    the condition and pick up the newest frame. Old frames are simply
    overwritten — a slow client skips frames instead of building a backlog."""

    def __init__(self):
        self.frame = None
        self.last_write = 0.0
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.last_write = time.monotonic()
            self.condition.notify_all()
        # Recording tap. offer() only appends to a bounded deque - no disk
        # work happens on this thread. bytes() because the encoder may reuse
        # the buffer, and a queued frame can sit for a second before it is
        # written, unlike the live frame which is read immediately.
        if recorder._running:
            recorder.offer(bytes(buf))

    def age(self):
        """Seconds since the last frame. Large means the pipeline stalled —
        which is invisible to an <img> tag, because a stream that stops
        producing frames never fires an error, it just freezes."""
        if not self.last_write:
            return None
        return time.monotonic() - self.last_write


_output = StreamingOutput()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        # startswith, not ==, because the page appends a cache-buster
        # (/stream?t=123). An exact match 404s every real request.
        if self.path.startswith('/stream'):
            self.send_response(200)
            self.send_header('Age', '0')
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            # Control page is served from :5000, stream from :8080 —
            # different origins, so be explicit rather than rely on the
            # browser's default leniency for <img>.
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type',
                             'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            # Nagle's algorithm holds small writes back to coalesce them - up
            # to 200ms on a link that is waiting for ACKs. Each frame is one
            # write anyway (below), but turn it off so nothing is ever held.
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            n = 0
            try:
                while True:
                    with _output.condition:
                        _output.condition.wait(timeout=5)
                        frame = _output.frame
                    if frame is None:
                        continue  # camera not producing yet
                    # During HD recording each frame is ~4x larger. Sending
                    # every one would multiply Wi-Fi load and latency, so the
                    # live view drops to a fraction of the frames. The
                    # recording is unaffected - it taps StreamingOutput.write.
                    n += 1
                    if _hd_active and n % HD_STREAM_EVERY:
                        continue
                    # One write per frame, not five. Fewer syscalls, and the
                    # boundary/header never goes out in its own tiny packet.
                    self.wfile.write(
                        b'--FRAME\r\nContent-Type: image/jpeg\r\n'
                        + f'Content-Length: {len(frame)}\r\n\r\n'.encode()
                        + frame + b'\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # viewer closed the page — normal, not an error
        elif self.path.startswith('/health'):
            age = _output.age()
            # pid is the important field: this server lives inside the Flask
            # process, so /health and /version MUST agree on it. They will not
            # if a second instance is running.
            body = json.dumps({
                "age": None if age is None else round(age, 2),
                "restarts": _restarts,
                "bitrate": _current_bitrate,
                "recording": recorder._running,
                "hd": _hd_active,
                "hd_available": bool(_cfg.get("_hd_profile")),
                "hflip": _cfg.get("hflip", 0),
                "vflip": _cfg.get("vflip", 0),
                "size": [_cfg.get("width"), _cfg.get("height")],
                "sensor": _cfg.get("sensor_size"),
                "model": _cfg.get("_sensor"),
                "lens": _cfg.get("lens_position"),
                "stamp": STAMP,
                "pid": os.getpid(),
                "role": "stream",
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/record') or self.path.startswith('/recordings'):
            # A cancelled download or a closed listing page raises here.
            # Normal client behaviour, not a fault - swallow it rather than
            # dumping a traceback into the journal on every tap.
            try:
                if not _recording_http(self, _cfg):
                    self.send_response(404)
                    self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep the robot console clean


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


_picam2 = None
_server = None
_restarts = 0
_cfg = {}
_watchdog = None
STALL_SECONDS = 8.0
# Bits per second for the hardware JPEG encoder. At 640x480 / 25fps, 4M gave
# ~20KB frames and 2.5M was about half that. The stream is 1024x576 now -
# roughly 2.4x the pixels - so 2.5M would spend the same bits on more area
# and look blocky. 4M restores about the same bits-per-pixel. Smaller frames
# still spend less time on 2.4GHz wifi, which is where the lag mostly lives,
# so drop this first if latency is worse than it was.
BITRATE = 4_000_000

# Bitrate used while recording. The stream value above was chosen to keep
# frames small on 2.4GHz wifi, which is a latency decision - recording has no
# such constraint, it writes to a local card. ~8M gives roughly 33KB frames
# at 640x480/25fps, or about 50MB per minute.
#
# This does NOT raise resolution. 640x480 is the sensor mode; more bits
# removes JPEG artefacts but adds no detail beyond that.
RECORD_BITRATE = 12_000_000

# Guards every stop/start of the encoder. The watchdog thread can also
# restart the pipeline, and the two must never overlap.
_camera_lock = Lock()
_current_bitrate = BITRATE

# Resolved from the detected camera profile at first open. Kept as module
# globals because set_bitrate() switches between them without re-detecting.
_stream_bitrate = BITRATE
_record_bitrate = RECORD_BITRATE


# ─── CAMERA PROFILES ─────────────────────────────────────────────────────────
# Two very different builds run this same code:
#
#   kit      Pi Zero W  + OV5647   single ARMv6 core, 512MB
#   personal Pi Zero 2W + IMX708   four A53 cores, 512MB
#
# Their right settings are not close to each other, and a member should never
# have to edit a file to get the right one. So the sensor picks its own.
#
#   OV5647 : 640x480 is a NATIVE binned mode. Anything else forces the ISP to
#            rescale, which measurably hurt on the Zero W. Do not "improve"
#            this - it was tested.
#   IMX708 : has no 640x480 at all. Its smallest mode, 1536x864, is a centre
#            crop of ~2/3 the sensor width, so the readout is pinned to
#            2304x1296 for full field of view and the ISP downscales to the
#            stream size. Focus is fixed at 0.5 dioptres (~2m), measured best
#            across mid and far on a lens-position sweep.
CAMERA_PROFILES = {
    "imx708": {
        "sensor_size": (2304, 1296),
        "width": 1024, "height": 576, "fps": 30,
        "lens_position": 0.5,
        "bitrate": 4_000_000,
        "record_bitrate": 12_000_000,
        # HD REC: same full-FOV readout, bigger output. 1080p is also the
        # largest size the hardware JPEG path is comfortable with.
        "hd": {"sensor_size": (2304, 1296), "width": 1920, "height": 1080,
               "fps": 30, "bitrate": 25_000_000},
    },
    # Raspberry Pi AI Camera (Sony IMX500). UNTESTED ON HARDWARE - added
    # 21 Sep 2026 so kit builds can use it. Needs the imx500-all package for
    # its sensor firmware, or it will not stream at all.
    "imx500": {
        # 2x2-binned full-FOV mode. The sensor is 4:3, not 16:9 like the CM3,
        # so the stream is 4:3 too - a 16:9 output would crop the top and
        # bottom off a view we deliberately kept whole.
        "sensor_size": (2028, 1520),
        "width": 960, "height": 720, "fps": 30,
        "lens_position": None,          # manual-focus lens, no motor
        "bitrate": 4_000_000,
        "record_bitrate": 12_000_000,
        "hd": {"sensor_size": (2028, 1520), "width": 1440, "height": 1080,
               "fps": 30, "bitrate": 25_000_000},
        # The camera loads firmware when it starts, and how long that takes on
        # a Zero 2 W is unmeasured. Without extra grace the watchdog could call
        # a slow start "never delivered a frame" and restart it mid-load. 30s
        # is a guess on the safe side; shorten once measured.
        "start_grace": 30.0,
    },
    "ov5647": {
        "sensor_size": None,
        "width": 640, "height": 480, "fps": 25,
        "lens_position": None,
        "bitrate": 2_500_000,
        "record_bitrate": 8_000_000,
        # HD REC: the 2x2-binned full-FOV mode, recorded at native size so the
        # ISP does not rescale - the thing that hurt on the Zero W. ~4x the
        # pixels of the stream. Frame rate on a Zero W is UNMEASURED; drop
        # "fps" here first if HD recordings come back with many drops.
        "hd": {"sensor_size": (1296, 972), "width": 1296, "height": 972,
               "fps": 25, "bitrate": 16_000_000},
    },
}
# Used when the sensor is neither of the above. Conservative on purpose: a
# small frame at a modest rate is the setting most likely to just work.
# No HD for an unknown sensor: its modes are a guess, and a wrong guess
# costs a sensor reconfigure on the one camera nobody has tested.
FALLBACK_PROFILE = dict(CAMERA_PROFILES["ov5647"], hd=None)


def _detect_profile(picam2):
    """Pick a profile from the sensor actually attached.

    Matches on the model name reported by libcamera, then sanity-checks that
    the profile's sensor mode really exists before trusting it."""
    model = ""
    try:
        model = str(picam2.camera_properties.get("Model", "")).lower()
    except Exception:
        pass

    for key, prof in CAMERA_PROFILES.items():
        if key in model:
            return key, dict(prof)

    # Unknown sensor. Fall back on mode geometry rather than guessing a name.
    try:
        sizes = {tuple(m["size"]) for m in picam2.sensor_modes}
    except Exception:
        sizes = set()
    if (640, 480) in sizes:
        return model or "unknown", dict(CAMERA_PROFILES["ov5647"])
    return model or "unknown", dict(FALLBACK_PROFILE)


def _open_camera(bitrate=None):
    """Bring up the sensor and the hardware encoder. Caller handles failure."""
    global _picam2, _current_bitrate, _stream_bitrate, _record_bitrate, _opened_at
    _picam2 = Picamera2()

    # Detect once, on the first open. A watchdog restart must not re-detect -
    # the sensor cannot have changed, and re-deciding mid-session would let a
    # transient read swap the settings under a running stream.
    if not _cfg.get("_profile_done"):
        name, prof = _detect_profile(_picam2)
        # Anything the caller passed explicitly wins over the profile; None
        # means "you decide". That keeps start() overridable for bench tests
        # without making the normal path carry any arguments at all.
        for key in ("sensor_size", "width", "height", "fps", "lens_position"):
            if _cfg.get(key) is None:
                _cfg[key] = prof[key]
        _stream_bitrate = prof["bitrate"]
        _record_bitrate = prof["record_bitrate"]
        _cfg["_profile_done"] = True
        _cfg["_sensor"] = name
        _cfg["_hd_profile"] = prof.get("hd")
        # 0 = no extra grace: sensors without a start_grace keep exactly the
        # timings they had before the AI Camera profile existed.
        _cfg["_start_grace"] = float(prof.get("start_grace") or 0.0)
        print(f"[CAM] sensor {name} -> {_cfg['width']}x{_cfg['height']}"
              f"@{_cfg['fps']} sensor={_cfg['sensor_size']} "
              f"lens={_cfg['lens_position']} bitrate={_stream_bitrate}", flush=True)

    if bitrate is None:
        bitrate = _stream_bitrate
    # buffer_count is the pipeline depth. The default queues several frames,
    # and every queued frame is latency you cannot get back - by the time one
    # reaches the phone the robot has already moved. Two is the shallowest
    # that still lets capture and encode overlap.
    ctrls = {"FrameRate": _cfg["fps"]}

    # Focus lock. The CM3 has a voice-coil lens; the OV5647 has no focus
    # motor at all, so only send these when the sensor actually reports
    # LensPosition - configure() raises on a control the camera lacks.
    #
    # LensPosition is dioptres (1/metres): 0.5 is 2m, 2.0 is 0.5m, 0 is
    # infinity. Measured sweep on this build put 0.5 best across mid and
    # far; left to itself autofocus locks near (~0.6m) and hunts while
    # driving, which is worse than a fixed setting in every respect.
    lens = _cfg.get("lens_position")
    if lens is not None and "LensPosition" in _picam2.camera_controls:
        ctrls["AfMode"] = _AF_MANUAL
        ctrls["LensPosition"] = float(lens)

    cfg_kwargs = dict(
        main={"size": (_cfg["width"], _cfg["height"]), "format": "YUV420"},
        controls=ctrls,
        transform=Transform(hflip=_cfg["hflip"], vflip=_cfg["vflip"]),
        buffer_count=2,
        queue=False,
    )

    # Pin the sensor readout mode, separately from the stream size.
    #
    # Without this, picamera2 picks the sensor mode closest to main[]. On the
    # CM3 that lands on 1536x864, which is a CENTRE CROP of roughly two thirds
    # the sensor width - a third of the field of view silently gone. Naming
    # the raw size keeps the full frame and lets the ISP downscale to the
    # stream size, so FOV and bandwidth stop being the same decision.
    #
    # Guarded by the mode list: an OV5647 build has no 2304x1296 mode, and
    # asking for one would land on 2592x1944 instead of failing loudly.
    sensor = _cfg.get("sensor_size")
    if sensor:
        available = {tuple(m["size"]) for m in _picam2.sensor_modes}
        if tuple(sensor) in available:
            cfg_kwargs["raw"] = {"size": tuple(sensor)}
        else:
            print(f"[CAM] sensor mode {tuple(sensor)} unavailable "
                  f"{sorted(available)} - letting picamera2 choose", flush=True)

    config = _picam2.create_video_configuration(**cfg_kwargs)
    _picam2.configure(config)
    # MJPEGEncoder is the hardware JPEG path on the Zero's VC4 —
    # the CPU does not compress frames.
    _picam2.start_recording(MJPEGEncoder(bitrate=bitrate), FileOutput(_output))
    _current_bitrate = bitrate
    _opened_at = time.monotonic()


def _wait_for_frame(timeout=5.0):
    """Block until a frame newer than now arrives. True if one did."""
    deadline = time.monotonic() + timeout
    with _output.condition:
        while time.monotonic() < deadline:
            if _output.condition.wait(timeout=0.5):
                return True
            if _output.frame is not None and _output.age() is not None \
                    and _output.age() < 0.5:
                return True
    return False


def set_bitrate(high):
    """Switch the encoder between stream and recording quality.

    Returns the bitrate actually in effect, so the caller can report what
    happened rather than assume it worked. Blocks for the ~1-2s the encoder
    takes to come back, and waits for a real frame before returning - the
    recorder must not start capturing during the gap or the first second of
    every clip would be missing.
    """
    global _current_bitrate
    want = _record_bitrate if high else _stream_bitrate

    with _camera_lock:
        if _picam2 is None or want == _current_bitrate:
            return _current_bitrate
        try:
            _picam2.stop_recording()
            _picam2.start_recording(MJPEGEncoder(bitrate=want), FileOutput(_output))
            _current_bitrate = want
            with _output.condition:
                _output.last_write = time.monotonic()
        except Exception as exc:
            print(f"[CAM] bitrate switch to {want} failed: {exc}", flush=True)
            # Try to get *some* pipeline back rather than leaving the robot
            # blind. The watchdog would eventually do this anyway, but that
            # is 8 seconds of black screen.
            try:
                _close_camera()
                time.sleep(0.5)
                _open_camera(_stream_bitrate)
                with _output.condition:
                    _output.last_write = time.monotonic()
                print("[CAM] recovered at stream bitrate", flush=True)
            except Exception as exc2:
                print(f"[CAM] recovery failed: {exc2}", flush=True)
            return _current_bitrate

    ok = _wait_for_frame()
    print(f"[CAM] bitrate now {_current_bitrate}"
          f"{'' if ok else ' (no frame yet)'}", flush=True)
    return _current_bitrate


def current_bitrate():
    return _current_bitrate


# ─── FOCUS ────────────────────────────────────────────────────────────────────
# LensPosition is dioptres (1/metres): 0 = infinity, 0.5 = 2 m, 4.0 = 25 cm.
# _open_camera() locks AfMode to Manual at _cfg["lens_position"], and everything
# here puts it BACK to Manual when it is done. Leaving the lens in continuous
# autofocus makes it hunt while driving, which is the failure this build
# deliberately designed out - a one-shot sweep that then locks is the useful
# version of the feature.
#
# Both entry points return (ok, value_or_reason) instead of raising, because the
# caller is an HTTP route and a traceback tells the user nothing.

def focus_available():
    """True only when a lens motor really exists. The OV5647 has none."""
    try:
        return bool(_picam2 is not None and "LensPosition" in _picam2.camera_controls)
    except Exception:
        return False


def focus_state():
    """11g: also reports the lens's own range, so the page's slider covers all
    of it (it was hard-coded to 0-4 dioptres; the CM3 goes much further)."""
    st = {"available": focus_available(), "lens": _cfg.get("lens_position")}
    if st["available"]:
        st["min"], st["max"] = _lens_range()
    return st


def _lens_range():
    """Ask the sensor for its own limits rather than assuming 0-10."""
    try:
        rng = _picam2.camera_controls.get("LensPosition")
        if rng and len(rng) >= 2 and rng[0] is not None and rng[1] is not None:
            return float(rng[0]), float(rng[1])
    except Exception:
        pass
    return 0.0, 10.0


def set_focus(lens):
    """Move the lens to a fixed dioptre value and leave it there."""
    if not focus_available():
        return False, "this sensor has no focus motor"
    try:
        lens = float(lens)
    except (TypeError, ValueError):
        return False, "lens must be a number"
    lo, hi = _lens_range()
    lens = max(lo, min(hi, lens))
    try:
        _picam2.set_controls({"AfMode": _AF_MANUAL, "LensPosition": lens})
    except Exception as exc:
        return False, f"lens write failed: {exc}"
    _cfg["lens_position"] = round(lens, 3)
    return True, _cfg["lens_position"]


def autofocus_once(timeout=3.0):
    """Run ONE autofocus sweep, then lock the lens where it landed.

    Not a mode change. If the sweep never reports a position, the previous
    setting is restored rather than the camera being left in Auto - otherwise
    a failed tap would silently turn on the hunting behaviour.
    """
    if not focus_available():
        return False, "this sensor has no focus motor"
    previous = _cfg.get("lens_position")
    try:
        _picam2.set_controls({"AfMode": _AF_AUTO, "AfTrigger": _AF_TRIGGER_START})
    except Exception as exc:
        return False, f"autofocus not supported: {exc}"

    deadline = time.time() + timeout
    found = None
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            md = _picam2.capture_metadata()
        except Exception:
            break
        pos = md.get("LensPosition")
        # AfState: 2 = Focused, 3 = Failed. A "failed" sweep still parks the
        # lens somewhere deliberate, so take the position either way and stop
        # waiting - the alternative is sitting here for the full timeout.
        if pos is not None and md.get("AfState") in (2, 3):
            found = float(pos)
            break

    if found is None:
        if previous is not None:
            try:
                _picam2.set_controls({"AfMode": _AF_MANUAL,
                                      "LensPosition": float(previous)})
            except Exception:
                pass
        return False, "autofocus did not settle - lens left where it was"

    try:
        _picam2.set_controls({"AfMode": _AF_MANUAL, "LensPosition": found})
    except Exception as exc:
        return False, f"lens lock failed: {exc}"
    _cfg["lens_position"] = round(found, 3)
    return True, _cfg["lens_position"]


# ─── STILL CAPTURE ────────────────────────────────────────────────────────────
# One photo at the sensor's full 4608x2592, via a mode switch.
#
# The alternative - a second permanent ISP output at the binned size - was
# rejected 20 Sep 2026: it taxes every frame of a pipeline that is already
# tight on a Zero 2 W, to serve a button pressed a few times a session.
#
# switch_mode_and_capture_file is used deliberately over a hand-rolled
# stop/configure/start/reconfigure sequence: it is the library's supported
# path, it restores the video configuration itself, and it costs TWO sensor
# reconfigurations instead of three. That matters here - reconfiguring the
# sensor is the operation that has twice left this CM3 not answering I2C
# until a power cycle, so every avoidable write is worth avoiding.
#
# Guards, in order:
#   - _camera_lock for the whole operation, so a second tap, a bitrate switch
#     or the watchdog cannot reconfigure underneath us. _watch() re-reads the
#     frame age INSIDE the same lock, so it will correctly decide no restart
#     is needed once we are done.
#   - transform is carried over explicitly. The stream runs hflip+vflip, and a
#     still configuration does not inherit it - without this the photo comes
#     out upside down.
#   - the focus lock is re-applied after the switch back, because configure()
#     resets controls and would otherwise undo whatever the FOC slider set.
#   - any exception falls through to a full close/open heal, so a failed shot
#     leaves a working stream rather than a black screen.

STILL_DIR = os.path.expanduser("~/recordings")


def _still_size():
    """Largest mode the sensor actually offers, not a hardcoded guess."""
    try:
        modes = [tuple(m["size"]) for m in _picam2.sensor_modes]
    except Exception:
        modes = []
    if not modes:
        return None
    return max(modes, key=lambda wh: wh[0] * wh[1])


def capture_still():
    """Take one full-resolution photo. Returns (ok, filename_or_reason).

    Blocks for several seconds and stalls the stream while it runs. That is
    inherent to a mode switch, not a bug - the caller should say so.
    """
    if _picam2 is None:
        return False, "camera is not running"

    size = _still_size()
    if size is None:
        return False, "could not read the sensor mode list"

    try:
        os.makedirs(STILL_DIR, exist_ok=True)
    except Exception as exc:
        return False, f"cannot create {STILL_DIR}: {exc}"

    name = time.strftime("zerobot_%Y%m%d_%H%M%S") + ".jpg"
    path = os.path.join(STILL_DIR, name)

    with _camera_lock:
        if _picam2 is None:
            return False, "camera is not running"
        try:
            still_cfg = _picam2.create_still_configuration(
                main={"size": size},
                transform=Transform(hflip=_cfg.get("hflip", 0),
                                    vflip=_cfg.get("vflip", 0)),
                buffer_count=1,
            )
            _picam2.switch_mode_and_capture_file(still_cfg, path)
        except Exception as exc:
            print(f"[CAM] still capture failed: {exc} - healing pipeline",
                  flush=True)
            try:
                os.remove(path)
            except Exception:
                pass
            _close_camera()
            time.sleep(1.0)
            try:
                _open_camera(_current_bitrate)
            except Exception as exc2:
                return False, f"capture failed ({exc}) and restart failed ({exc2})"
            return False, f"capture failed: {exc}"

        # configure() reset the controls on the way back. Put the focus lock
        # where the user last left it, not where the profile default says.
        lens = _cfg.get("lens_position")
        if lens is not None:
            try:
                if "LensPosition" in _picam2.camera_controls:
                    _picam2.set_controls({"AfMode": _AF_MANUAL,
                                          "LensPosition": float(lens)})
            except Exception:
                pass

    if not _wait_for_frame(timeout=_first_frame_timeout()):
        print("[CAM] no frames after still capture - healing pipeline", flush=True)
        with _camera_lock:
            _close_camera()
            time.sleep(1.0)
            try:
                _open_camera(_current_bitrate)
            except Exception as exc:
                return True, name   # the photo is on disk; the stream is not
        return True, name

    try:
        mb = round(os.path.getsize(path) / 1e6, 2)
    except Exception:
        mb = None
    print(f"[CAM] still {size[0]}x{size[1]} -> {name} ({mb} MB)", flush=True)
    return True, name


# ─── HD RECORDING ─────────────────────────────────────────────────────────────
# A separate, heavier recording mode. Normal REC only raises the bitrate of
# the stream it is already running; HD REC reconfigures the sensor to the
# profile's "hd" mode for the length of the clip, then puts the stream mode
# back. Cost is paid only while recording - the same rule as the photo path.
#
# Every failure path ends with the STREAM profile running. An HD start that
# cannot be honoured must never leave the robot blind.

HD_STREAM_EVERY = 4    # live view sends 1 frame in N during HD recording
_hd_active = False
_HD_KEYS = ("sensor_size", "width", "height", "fps")


def _running_mode():
    """What is actually configured now - picamera2 may have adjusted sizes."""
    try:
        w, h = _picam2.camera_configuration()["main"]["size"]
    except Exception:
        w, h = _cfg.get("width"), _cfg.get("height")
    return {"width": int(w), "height": int(h),
            "fps": float(_cfg.get("fps") or 25),
            "bitrate": _current_bitrate}


def _restore_normal_cfg():
    normal = _cfg.pop("_normal", None)
    if normal:
        for k in _HD_KEYS:
            _cfg[k] = normal[k]


def set_hd(on):
    """Enter or leave HD recording mode.

    Returns the running mode as {"width","height","fps","bitrate"}, or None
    if HD could not start - in which case the stream profile has already been
    restored. Blocks for the few seconds a sensor reconfigure takes.
    """
    global _hd_active
    hd = _cfg.get("_hd_profile")
    if on and not hd:
        return None

    with _camera_lock:
        if _picam2 is None:
            return None
        if bool(on) == _hd_active:
            return _running_mode()

        if on:
            _cfg["_normal"] = {k: _cfg.get(k) for k in _HD_KEYS}
            for k in _HD_KEYS:
                _cfg[k] = hd[k]
            want = hd["bitrate"]
        else:
            _restore_normal_cfg()
            want = _stream_bitrate

        _close_camera()
        time.sleep(0.5)
        try:
            _open_camera(want)
            _hd_active = bool(on)
        except Exception as exc:
            print(f"[CAM] HD {'start' if on else 'stop'} failed: {exc} "
                  f"- restoring stream mode", flush=True)
            _restore_normal_cfg()
            _hd_active = False
            _close_camera()
            time.sleep(0.5)
            try:
                _open_camera(_stream_bitrate)
            except Exception as exc2:
                print(f"[CAM] stream restore failed: {exc2}", flush=True)
            return None

    if not _wait_for_frame(timeout=_first_frame_timeout()):
        print("[CAM] no frame yet after HD switch - watchdog will act if it "
              "stays dark", flush=True)
    mode = _running_mode()
    print(f"[CAM] HD {'on' if on else 'off'}: {mode['width']}x{mode['height']}"
          f"@{mode['fps']:g} {mode['bitrate']} bps", flush=True)
    return mode


def _close_camera():
    global _picam2
    if _picam2:
        try:
            _picam2.stop_recording()
        except Exception:
            pass
        try:
            _picam2.close()
        except Exception:
            pass
        _picam2 = None


_opened_at = 0.0     # monotonic time of the last successful _open_camera()
_stopping = False    # set by stop(); the watchdog must not revive a camera
                     # that was closed on purpose


def _first_frame_timeout():
    """How long a freshly opened pipeline gets to produce its first frame.

    6s normally. Longer for a sensor whose profile declares a start_grace
    (the AI Camera loads firmware on start). Used everywhere a restart is
    verified, so a slow starter is not declared dead mid-load.
    """
    return max(6.0, float(_cfg.get("_start_grace") or 0.0))


def _stall_seconds():
    """How long the pipeline has gone without frames, or None if healthy.

    Two different failures:
      - frames STOPPED: age() grows past STALL_SECONDS.
      - frames NEVER STARTED: age() is None because no frame has arrived in
        this process at all. Before 21 Sep 2026 this case was skipped, so a
        camera that configured but delivered nothing sat silent with
        restarts = 0 forever. Measured from the last open instead.
    """
    age = _output.age()
    if age is not None:
        return age if age >= STALL_SECONDS else None
    if not _opened_at:
        return None                      # never opened: nothing to watch yet
    waited = time.monotonic() - _opened_at
    grace = max(STALL_SECONDS, float(_cfg.get("_start_grace") or 0.0))
    return waited if waited >= grace else None


def _watch():
    """Restart the pipeline if frames stop arriving - or never arrive.

    The HTTP server keeps serving and the browser keeps holding the
    connection open, so nothing downstream notices a stalled encoder.
    Only this thread can see it.

    retry: a restart failed and the camera is CLOSED. The old loop began with
    "camera closed? skip", so one failed restart silenced it for good and the
    backoff never ran past its first step. retry keeps it trying, spaced out,
    until a restart actually produces a frame.
    """
    global _restarts
    backoff = 0.0
    retry = False
    while True:
        time.sleep(2.0)
        # Slept OUTSIDE _camera_lock so a photo or a bitrate switch is not
        # blocked behind a camera that is already dead.
        if backoff:
            time.sleep(backoff)
        if _stopping:
            return

        if retry:
            print("[WATCHDOG] Retrying camera after a failed restart", flush=True)
        else:
            if _picam2 is None:
                continue                 # not started yet
            stalled = _stall_seconds()
            if stalled is None:
                continue
            print(f"[WATCHDOG] No camera frame for {stalled:.1f}s - "
                  f"restarting pipeline", flush=True)

        with _camera_lock:
            if _stopping:
                return
            # Re-check inside the lock: a bitrate switch, HD switch or photo
            # may have been running when we looked, and be healthy now.
            if not retry and _stall_seconds() is None:
                print("[WATCHDOG] Frames resumed - no restart needed", flush=True)
                continue

            _close_camera()
            time.sleep(1.0)
            _restarts += 1
            try:
                _open_camera(_current_bitrate)
            except Exception as e:
                _close_camera()
                retry = True
                backoff = min(max(backoff * 2, STALL_SECONDS), 120.0)
                print(f"[WARN] Restart #{_restarts} raised: {e} - next try in "
                      f"{backoff:.0f}s", flush=True)
                continue

            # A call that did not throw is NOT a restart that worked. Ask for
            # an actual frame. (Old code faked a fresh timestamp here, which
            # is how 89 failed restarts on 17 Sep 2026 all logged "restarted".)
            if _wait_for_frame(timeout=_first_frame_timeout()):
                retry = False
                backoff = 0.0
                print(f"[OK] Camera pipeline restarted, frames flowing "
                      f"(restart #{_restarts})", flush=True)
            else:
                _close_camera()
                retry = True
                backoff = min(max(backoff * 2, STALL_SECONDS), 120.0)
                print(f"[WARN] Restart #{_restarts} opened the camera but no "
                      f"frame arrived - the sensor is not answering. Next try "
                      f"in {backoff:.0f}s. If this repeats, check the camera "
                      f"hardware; a POWER CYCLE clears a stuck sensor.",
                      flush=True)


def start(width=None, height=None, fps=None, port=8080, hflip=0, vflip=0,
          sensor_size=None, lens_position=None):
    """Start the camera and the stream server. Returns True on success.
    Failure is graceful: the robot runs without video and says why.

    hflip/vflip set the sensor orientation at init, so the ISP does the work
    for free. Rotating in the browser instead would cost the phone a
    full-frame repaint at the frame rate. The face-plate mount is inverted,
    so this build passes hflip=1, vflip=1 for 180 degrees.

    A 90 degree mount cannot be corrected here - libcamera has no rotate-90
    on this path - and would have to be done in the page after all."""
    global _picam2, _server

    if not PICAMERA2_AVAILABLE:
        print("[WARN] picamera2 not installed - no video.")
        print("       Fix: sudo apt-get install -y python3-picamera2")
        return False

    _cfg.update(width=width, height=height, fps=fps, hflip=hflip, vflip=vflip,
                sensor_size=sensor_size, lens_position=lens_position)

    try:
        _open_camera()
    except Exception as e:
        print(f"[WARN] Camera failed to start: {e}")
        print("       Robot will run WITHOUT video.")
        print("       After fixing the camera, restart with: bash start.sh")
        _picam2 = None
        return False

    # Let the recorder raise quality while it runs. A hook rather than an
    # import, because recorder.py is imported by this module - importing back
    # the other way would be circular.
    _recorder_mod.quality_hook = set_bitrate
    _recorder_mod.hd_hook = set_hd

    _server = StreamingServer(('', port), StreamingHandler)
    Thread(target=_server.serve_forever, daemon=True).start()

    global _watchdog
    if _watchdog is None:
        _watchdog = Thread(target=_watch, daemon=True)
        _watchdog.start()

    print(f"[OK] Camera streaming on port {port} (hardware MJPEG encode) - {STAMP}")
    print(f"[OK] Sensor transform applied: hflip={hflip} vflip={vflip}")
    print(f"[OK] Camera watchdog armed - restarts after {STALL_SECONDS:.0f}s without a frame")
    return True


def stop():
    """Best-effort clean shutdown."""
    global _server, _stopping
    _stopping = True
    _close_camera()
    if _server:
        _server.shutdown()
        _server = None
