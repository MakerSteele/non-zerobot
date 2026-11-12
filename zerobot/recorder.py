#!/usr/bin/env python3
"""Recording: writes the stream's existing JPEG frames to an AVI on disk.

Design constraint that drives everything here: **recording must never slow
the stream down.** The encoder thread calls offer() 25 times a second, and
that call does nothing but drop a reference into a bounded deque. All disk
work happens on a separate thread. If the SD card falls behind, frames are
dropped from the recording and the live stream is untouched - the robot
stays drivable, which matters more than a perfect video file.

Files land in ~/recordings, deliberately outside ~/zerobot so that
`rm -rf ~/zerobot` during a deploy cannot delete your footage. Same
reasoning as ~/.zerobot_tune.json.
"""

import html
import json
import os
import re
import shutil
import time
from collections import deque
from datetime import datetime
from threading import Condition, Thread

from mjpeg_avi import AviWriter

RECORD_DIR = os.path.expanduser('~/recordings')

# Refuse to start below this, and auto-stop if we fall below it while running.
# A full SD card on a Pi is not a graceful failure - it can corrupt the
# filesystem and take the whole robot down, so this is a safety limit rather
# than a convenience.
MIN_FREE_MB = 150

# Hard cap. At roughly 270 KB/s a 20 minute clip is about 320 MB.
MAX_SECONDS = 20 * 60

# Queue depth. ~1.2s of buffer at 25fps. Deeper just hides a card that cannot
# keep up while using more of the Zero's 512MB.
QUEUE_MAX = 30

# Second-resolution timestamps collide if two recordings start within the
# same second, and the second one would silently overwrite the first. The
# optional _N suffix below is what _unique_path() adds to prevent that.
# Set by camera.py at startup: set_bitrate(high: bool) -> int.
# A hook rather than an import, to keep camera <-> recorder one-directional.
# None when running headless or under test, in which case quality switching
# is simply skipped.
quality_hook = None

# Set by camera.py at startup: set_hd(on: bool) -> dict | None. Reconfigures
# the sensor for HD recording and returns the mode actually running, so the
# AVI header gets the real frame size rather than the one that was asked for.
hd_hook = None


def _set_hd(on):
    """Enter or leave HD mode. Never fatal; None means it did not happen."""
    if hd_hook is None:
        return None
    try:
        return hd_hook(on)
    except Exception as exc:
        print(f"[REC] HD switch failed: {exc}", flush=True)
        return None


def _set_quality(high):
    """Ask the camera for record or stream quality. Never fatal."""
    if quality_hook is None:
        return None
    try:
        return quality_hook(high)
    except Exception as exc:
        print(f"[REC] quality switch failed: {exc}", flush=True)
        return None


# Single gate for both the listing and the download handler, so widening it
# here is what makes stills appear in FILES. Still a strict whitelist: nothing
# this program did not create is ever served.
_NAME_RE = re.compile(r'^zerobot_\d{8}_\d{6}(?:_\d+)?\.(?:avi|jpg)$')


def _free_mb(path):
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except Exception:
        return None


def _disk_mb(path):
    """(free, total) in MB for the card holding path.

    Falls back to the home directory before the first recording has created
    RECORD_DIR - same card, and the meter should work from the first page load.
    """
    if not os.path.isdir(path):
        path = os.path.expanduser('~')
    try:
        u = shutil.disk_usage(path)
        return u.free / (1024 * 1024), u.total / (1024 * 1024)
    except Exception:
        return None, None


def _unique_path():
    """A path that does not already exist.

    Timestamps are only accurate to the second, so stopping and immediately
    restarting produces the same name and would overwrite the clip just
    finished. Verified: this happened before the suffix was added.
    """
    base = datetime.now().strftime('zerobot_%Y%m%d_%H%M%S')
    path = os.path.join(RECORD_DIR, base + '.avi')
    n = 2
    while os.path.exists(path):
        path = os.path.join(RECORD_DIR, f'{base}_{n}.avi')
        n += 1
    return path


class Recorder:
    def __init__(self):
        self._cond = Condition()
        self._queue = deque()
        self._writer = None
        self._thread = None
        self._running = False
        self._path = None
        self._started = 0.0
        self._frames = 0
        self._dropped = 0
        self._bytes = 0
        self._bitrate = None
        self._stop_reason = None
        self._hd = False

    # ── called from the encoder thread - must stay trivial ──────────────
    def offer(self, jpeg):
        if not self._running:
            return
        with self._cond:
            if len(self._queue) >= QUEUE_MAX:
                self._dropped += 1          # card behind; protect the stream
                return
            self._queue.append(jpeg)
            self._cond.notify()

    # ── control ─────────────────────────────────────────────────────────
    def start(self, width=640, height=480, fps=25.0, hd=False):
        if self._running:
            return False, "already recording"

        try:
            os.makedirs(RECORD_DIR, exist_ok=True)
        except Exception as exc:
            return False, f"cannot create {RECORD_DIR}: {exc}"

        free = _free_mb(RECORD_DIR)
        if free is not None and free < MIN_FREE_MB:
            return False, f"only {free:.0f}MB free, need {MIN_FREE_MB}MB"

        path = _unique_path()
        name = os.path.basename(path)

        # Raise encoder quality BEFORE the writer opens. The switch restarts
        # the encoder and takes a second or two; capturing during that gap
        # would put stream-quality frames at the head of every clip.
        if hd:
            # The sensor switch happens here, before the file opens, for the
            # same reason as the bitrate switch below: frames captured during
            # the reconfigure would be the wrong size for the AVI header.
            mode = _set_hd(True)
            if not mode:
                return False, "HD not available on this camera"
            width, height, fps = mode["width"], mode["height"], mode["fps"]
            bitrate = mode["bitrate"]
        else:
            bitrate = _set_quality(True)
        try:
            self._writer = AviWriter(path, width, height, fps)
        except Exception as exc:
            self._writer = None
            # don't leave the camera boosted or in HD
            if hd:
                _set_hd(False)
            else:
                _set_quality(False)
            return False, f"cannot open file: {exc}"
        self._hd = bool(hd)

        self._path = path
        self._queue.clear()
        self._frames = self._dropped = self._bytes = 0
        self._stop_reason = None
        self._started = time.monotonic()
        self._running = True
        self._bitrate = bitrate
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[REC] recording to {name} at {bitrate or 'default'} bps", flush=True)
        return True, name

    def stop(self, reason="stopped"):
        if not self._running:
            return False, "not recording"
        self._stop_reason = reason
        self._running = False
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t:
            t.join(timeout=5.0)
        self._thread = None
        return True, os.path.basename(self._path or '')

    # ── writer thread ───────────────────────────────────────────────────
    def _run(self):
        last_check = time.monotonic()
        try:
            while True:
                with self._cond:
                    while self._running and not self._queue:
                        self._cond.wait(timeout=0.5)
                    if not self._running and not self._queue:
                        break
                    frame = self._queue.popleft() if self._queue else None

                if frame is not None:
                    try:
                        if self._writer.write_frame(frame):
                            self._frames += 1
                            self._bytes += len(frame)
                    except Exception as exc:
                        print(f"[REC] write failed: {exc}", flush=True)
                        self._stop_reason = f"write error: {exc}"
                        self._running = False
                        break

                now = time.monotonic()
                if now - last_check >= 5.0:
                    last_check = now
                    if now - self._started > MAX_SECONDS:
                        self._stop_reason = "duration limit"
                        self._running = False
                    free = _free_mb(RECORD_DIR)
                    if free is not None and free < MIN_FREE_MB:
                        self._stop_reason = f"low disk ({free:.0f}MB)"
                        self._running = False
        finally:
            elapsed = max(time.monotonic() - self._started, 0.001)
            actual = self._frames / elapsed if self._frames else None
            try:
                if self._writer:
                    self._writer.close(actual_fps=actual)
            except Exception as exc:
                print(f"[REC] close failed: {exc}", flush=True)
            self._writer = None
            self._running = False
            # Drop the encoder back to stream quality. Done here rather than
            # in stop() so an automatic stop - duration cap, low disk, write
            # error - restores it too, not just the button.
            if self._hd:
                _set_hd(False)
                self._hd = False
            else:
                _set_quality(False)
            # flush=True matters: systemd block-buffers stdout, and without
            # it this summary never reaches journalctl. Its absence made a
            # working recorder look broken for an entire debugging round.
            print(f"[REC] {os.path.basename(self._path or '?')}: "
                  f"{self._frames} frames, {self._bytes/1e6:.1f}MB, "
                  f"{elapsed:.0f}s, {self._dropped} dropped "
                  f"({self._stop_reason or 'stopped'})", flush=True)

    # ── introspection ───────────────────────────────────────────────────
    def status(self):
        elapsed = (time.monotonic() - self._started) if self._running else 0.0
        free, total = _disk_mb(RECORD_DIR)
        return {
            "recording": self._running,
            "file": os.path.basename(self._path) if self._path else None,
            "seconds": round(elapsed, 1),
            "frames": self._frames,
            "megabytes": round(self._bytes / 1e6, 2),
            "dropped": self._dropped,
            "free_mb": round(free) if free is not None else -1,
            "total_mb": round(total) if total is not None else -1,
            # the recorder refuses to start / stops itself below this, so
            # "time left" in the UI is measured down to here, not to zero
            "min_free_mb": MIN_FREE_MB,
            "bitrate": self._bitrate,
            "hd": bool(self._hd and self._running),
            "max_seconds": MAX_SECONDS,
            "last_reason": self._stop_reason,
        }


recorder = Recorder()


# ── file listing ────────────────────────────────────────────────────────
def _list_files():
    try:
        names = [n for n in os.listdir(RECORD_DIR) if _NAME_RE.match(n)]
    except Exception:
        return []
    out = []
    for n in sorted(names, reverse=True):
        p = os.path.join(RECORD_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "mb": round(st.st_size / 1e6, 1),
                    "when": datetime.fromtimestamp(st.st_mtime).strftime('%d %b %H:%M')})
    return out


_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZeroBot recordings</title><style>
body{{background:#0a0c0f;color:#c8d6e5;font-family:ui-monospace,Menlo,Consolas,monospace;
padding:16px;margin:0}}
h1{{font-size:.9rem;letter-spacing:3px;color:#00e5ff;margin-bottom:4px}}
.sub{{font-size:.65rem;color:#4a5568;margin-bottom:16px}}
a{{color:#00e5ff;text-decoration:none}}
.row{{display:flex;align-items:center;gap:10px;padding:10px 8px;
border-bottom:1px solid #1e2530}}
.name{{flex:1;font-size:.75rem;word-break:break-all}}
.meta{{font-size:.6rem;color:#4a5568;white-space:nowrap}}
.del{{color:#ff3d00;font-size:.65rem}}
.empty{{color:#4a5568;font-size:.7rem;padding:20px 0}}
</style></head><body>
<h1>RECORDINGS</h1><div class="sub">{free} MB free &middot; {count} files</div>
{rows}
</body></html>"""


def _listing_html():
    files = _list_files()
    if files:
        rows = "".join(
            '<div class="row"><span class="name"><a href="/recordings/{n}">{n}</a></span>'
            '<span class="meta">{w} &middot; {m} MB</span>'
            '<a class="del" href="/recordings/{n}?delete=1">DEL</a></div>'.format(
                n=html.escape(f["name"]), w=html.escape(f["when"]), m=f["mb"])
            for f in files)
    else:
        rows = '<div class="empty">Nothing recorded yet.</div>'
    return _PAGE.format(free=round(_free_mb(RECORD_DIR) or -1),
                        count=len(files), rows=rows)


# ── HTTP ────────────────────────────────────────────────────────────────
def _send_json(h, obj, code=200):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Access-Control-Allow-Origin', '*')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    try:
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _send_html(h, text, code=200):
    body = text.encode()
    h.send_response(code)
    h.send_header('Content-Type', 'text/html; charset=utf-8')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    try:
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _parse_range(header, size):
    """Return (start, end) inclusive for a single-range request, or None.

    Only the common forms are handled - "bytes=0-", "bytes=500-999",
    "bytes=-1000". Multipart ranges are rare from media clients and are
    declined by returning None, which falls back to a normal 200.
    """
    if not header or not header.strip().lower().startswith('bytes='):
        return None
    spec = header.split('=', 1)[1].strip()
    if ',' in spec:
        return None
    first, _, last = spec.partition('-')
    try:
        if not first:                      # suffix range: last N bytes
            n = int(last)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def _send_file(h, full, name):
    """Serve a recording, honouring Range and tolerating a client hangup."""
    size = os.path.getsize(full)
    rng = _parse_range(h.headers.get('Range') if h.headers else None, size)

    if rng:
        start, end = rng
        length = end - start + 1
        h.send_response(206)
        h.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    else:
        start, length = 0, size
        h.send_response(200)

    h.send_header('Content-Type',
                  'image/jpeg' if name.lower().endswith('.jpg')
                  else 'video/x-msvideo')
    # inline, not attachment: a browser or player that can handle MJPEG AVI
    # should be allowed to just play it. Saving still works from the menu.
    h.send_header('Content-Disposition', f'inline; filename="{name}"')
    h.send_header('Accept-Ranges', 'bytes')
    h.send_header('Content-Length', str(length))
    h.send_header('Access-Control-Allow-Origin', '*')
    h.end_headers()

    # Chunked: a 300MB file must not be read into the Zero's 512MB of RAM.
    try:
        with open(full, 'rb') as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(64 * 1024, left))
                if not chunk:
                    break
                h.wfile.write(chunk)
                left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        # Client closed the connection - a paused download, a player that
        # got what it wanted, a phone locking. Normal, not an error.
        pass


def serve_http(h, cfg):
    """Handle /record* and /recordings*. Returns True if it took the request.

    Everything is GET so the control page (served from :5000) can call these
    cross-origin without a preflight. That is sloppy REST and a deliberate
    choice - the same one /paircheck already makes.
    """
    path = h.path.split('?', 1)[0]
    query = h.path[len(path) + 1:] if '?' in h.path else ''

    if path == '/record/start':
        hd = 'hd=1' in query.split('&')
        ok, msg = recorder.start(cfg.get('width', 640), cfg.get('height', 480),
                                 float(cfg.get('fps', 25)), hd=hd)
        out = recorder.status()
        out["ok"] = ok
        out["message"] = msg
        _send_json(h, out)
        return True

    if path == '/record/stop':
        ok, msg = recorder.stop()
        out = recorder.status()
        out["ok"] = ok
        out["message"] = msg
        _send_json(h, out)
        return True

    if path == '/record/status':
        _send_json(h, recorder.status())
        return True

    if path in ('/recordings', '/recordings/'):
        _send_html(h, _listing_html())
        return True

    if path.startswith('/recordings/'):
        name = os.path.basename(path[len('/recordings/'):])
        # Whitelist by pattern rather than trying to sanitise. Anything that
        # is not a file this program created is simply not served.
        if not _NAME_RE.match(name):
            _send_json(h, {"error": "bad name"}, 404)
            return True
        full = os.path.join(RECORD_DIR, name)
        if not os.path.isfile(full):
            _send_json(h, {"error": "not found"}, 404)
            return True

        if 'delete=1' in query:
            if recorder.status()["file"] == name and recorder.status()["recording"]:
                _send_json(h, {"error": "still recording"}, 409)
                return True
            try:
                os.remove(full)
            except Exception as exc:
                _send_json(h, {"error": str(exc)}, 500)
                return True
            h.send_response(303)
            h.send_header('Location', '/recordings')
            h.end_headers()
            return True

        _send_file(h, full, name)
        return True

    return False
