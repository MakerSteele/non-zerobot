#!/usr/bin/env python3
"""mic_live.py - LIVE robot microphone (robot -> phone).

13k (17 Nov 2026): TWO kinds of mic, picked automatically by probe():
  * I2S  - SPH0645 on GPIO 18/19/20 (pins 12/35/38), "adau7002" sound card
           (13l; the 13k "googlevoicehat" card still works but holds GPIO 16 =
           RGB headlight green). Clean digital mic: no hiss filters, just a 150 Hz low-cut,
           a fixed boost and a soft limiter (I2SDSP below). Needs the kernel
           6.18.60+ (6.18.50 recorded garbage at instant speed) and the
           pigpio <-> mic hand-over (pcm_swap.py through zerobot-root).
  * XIAO - the original MAX4466 + XIAO SAMD21 over the UART (everything below
           MicDSP). Kept as the fallback.
Override with ~/.zerobot_mic: "off", "xiao" or "i2s".

13g notes for the XIAO path follow.
mic_live.py 13g: 25 ms nap between serial reads (13f spun a whole core on tiny reads).

The same sound as mic_pi_test.py v9 (floor 0.15), but streaming:
  XIAO SAMD21 (MAX4466 on A0) -> UART 1,000,000 baud -> /dev/serial0 -> here
  -> DC removal -> 100 Hz comb notch -> Wiener hiss removal + voice gate
  -> low-pass 3800 / high-pass 250 -> 16 kHz 16-bit PCM chunks to listeners.

Differences from the offline v9 (which saw the whole recording at once):
  * The hiss estimate is LEARNED: the first 1 s after LISTEN starts is used
    as the hiss sample (stay quiet for it), then it keeps tracking slowly
    (40th percentile tracker, mostly frozen while a voice is present).
  * The voice gate looks 19 frames (~150 ms) ahead, so it is identical to
    v9 at the cost of ~150 ms extra delay.
  * While the robot is talking through its motors (voice.busy) the mic is
    muted (silence sent) + 0.4 s after, so it never hears itself. The DSP
    is skipped while muted, so it costs voice.py no CPU.

Runs ONLY while someone is listening (the serial port is closed otherwise).

Standalone test (robot.py may keep running - just don't tap LISTEN):
    python3 ~/zerobot/mic_live.py [seconds]
  -> ~/mic_live.wav + CPU use. Stay quiet for the first second.
"""
import os
import re
import sys
import time
import queue
import threading
import termios

import numpy as np

RATE = 16000
DEV = "/dev/serial0"
HOP = 128                 # samples per STFT hop = 8 ms
NFFT = 512
PERIOD = 160.056          # comb notch period (99.965 Hz in XIAO time)
NOTCH_G = 0.8
FLOOR = 0.15              # picked by ear, 13 Nov
LOWPASS = 3800.0
HIGHPASS = 250.0
ALPHA = 0.98              # decision-directed smoothing
PCT = 0.40                # hiss = 40th percentile per bin
HISS_X = 1.5              # x1.5 on top of it (same as v9)
WARM_FRAMES = 125         # 1 s learning the hiss at the start
ETA_QUIET = 0.02          # hiss tracker speed (log units/frame) when no voice
ETA_VOICE = 0.002         # ... and 10x slower while voice is present
HOLD = 15                 # gate held open +/-15 frames (120 ms)
SOFT = 4                  # soft edges = mean over +/-4 frames
LOOK = HOLD + SOFT        # look-ahead in frames
OUT_CHUNK = 1024          # samples per chunk sent to the phone (64 ms)
READ_NAP = 0.025           # s between serial reads (13g)
UNMUTE_HANG = 0.4         # s of silence after the motors stop talking
SCALE = 16.0              # 12-bit -> 16-bit, same as v9

# ── 13k: I2S mic (SPH0645) ──────────────────────────────────────────────────
# 13l: ALSA card names we accept, best first. adau7002-simple claims no GPIO;
# googlevoicehat-soundcard (13k) grabs GPIO 16 for an amp pin we don't have.
I2S_CARDS = ("adau7002", "sndrpigooglevoi")
I2S_CARD_BAD_GPIO = {"sndrpigooglevoi": 16}
I2S_RATE = 48000                      # native rate; we decimate by 3 to 16 kHz
I2S_GAIN = 50.0                       # +34 dB: 17 Nov clip, speech at -55 dBFS (13o: back from 13n's 17 - that report was about the phone's LISTEN volume)
I2S_SETTLE = 0.3                      # s thrown away after each start (mic drifts). 13p: 0.5 -> 0.3 for the intercom (back to 0.5 if a thump appears)
I2S_UNMUTE_HANG = 0.25                # 13p: s after the voice ends before the I2S mic restarts (XIAO keeps UNMUTE_HANG)
I2S_FADE = 0.05                       # s fade-in after the settle
I2S_HP = 150.0                        # Hz low-cut: 89 % of the room noise is below it
I2S_MIN_KERNEL = (6, 18, 60)          # 6.18.50 returned instant garbage
ROOT_HELPER = "/usr/local/sbin/zerobot-root"


class MicDSP:
    """Streaming version of mic_pi_test.py v9. feed(raw 12-bit ints) ->
    float samples (same scale as v9 before x16), delayed by about
    NFFT + LOOK*HOP samples."""

    def __init__(self):
        self.win = np.hanning(NFFT + 1)[:-1]
        f = np.fft.rfftfreq(NFFT, 1.0 / RATE)
        self.lowb = (f >= 150) & (f <= 1000)
        band = np.ones_like(f)
        if LOWPASS > 0:
            band *= np.clip((LOWPASS + 300.0 - f) / 300.0, 0.0, 1.0)
        if HIGHPASS > 0:
            band *= np.clip((f - HIGHPASS + 60.0) / 60.0, 0.0, 1.0)
        self.band = band
        # notch coefficients (4-point Lagrange fractional delay)
        m = int(PERIOD)
        t = PERIOD - m
        self.nm = m
        self.nc = (-t * (t - 1) * (t - 2) / 6, (t + 1) * (t - 1) * (t - 2) / 2,
                   -(t + 1) * t * (t - 2) / 2, (t + 1) * t * (t - 1) / 6)
        self.nk = (1.0 + NOTCH_G) / 2.0
        self.nh = m + 2                  # history length the notch needs
        self.logN = None                 # log of the 40th-percentile hiss per bin
        self.warm = []
        self.dc = None
        self.reset()

    def reset(self):
        """Clear the streaming buffers (keeps the learned hiss)."""
        self.pend_in = np.zeros(0)
        self.xh = np.zeros(self.nh)
        self.yh = np.zeros(self.nh)
        self.ibuf = np.zeros(NFFT)
        self.obuf = np.zeros(NFFT)
        self.prev = np.zeros(NFFT // 2 + 1)
        self.acts = [False] * LOOK       # frames -LOOK..-1 count as quiet
        self.frames = []                 # (S, Wiener gain) waiting for look-ahead
        self.quiet_run = 0

    # -- DC removal: v9's dc += (v - dc)/1024 per sample, done per block ----
    def _dc(self, x):
        if self.dc is None:
            self.dc = float(x[0])
        a = 1.0 / 1024.0
        b = 1.0 - a
        n = len(x)
        bp = b ** np.arange(1, n + 1)            # b^(i+1)
        # dc_i = b^(i+1) dc_prev + a * sum_k<=i b^(i-k) x_k
        acc = np.cumsum(x / (b ** np.arange(n))) * (b ** np.arange(n))
        d = bp * self.dc + a * acc
        self.dc = float(d[-1])
        return x - d

    # -- 100 Hz comb notch, vectorised (blocks <= 159 samples) ---------------
    def _notch(self, x):
        H = self.nh
        m = self.nm
        n = len(x)
        X = np.concatenate([self.xh, x])
        Y = self.yh
        c0, c1, c2, c3 = self.nc
        i = np.arange(n) + H
        xd = c0 * X[i - m + 1] + c1 * X[i - m] + c2 * X[i - m - 1] + c3 * X[i - m - 2]
        # y needed at delay >= m-1 = 159 > n-1, so all from history
        yd = c0 * Y[i - m + 1] + c1 * Y[i - m] + c2 * Y[i - m - 1] + c3 * Y[i - m - 2]
        y = self.nk * (x - xd) + NOTCH_G * yd
        self.xh = X[-H:]
        self.yh = np.concatenate([Y, y])[-H:]
        return y

    def _frame(self, hop_block):
        """One STFT frame in, one hop of output (or None while warming up)."""
        self.ibuf = np.concatenate([self.ibuf[HOP:], hop_block])
        S = np.fft.rfft(self.ibuf * self.win)
        P = S.real * S.real + S.imag * S.imag
        if self.logN is None:                    # still learning the hiss
            self.warm.append(P)
            if len(self.warm) >= WARM_FRAMES:
                self.logN = np.log(np.percentile(np.array(self.warm), PCT * 100, axis=0) + 1e-9)
                self.warm = []
            return None
        Nq = np.exp(self.logN)
        N = Nq * HISS_X + 1e-9
        post = P / N
        xi = ALPHA * self.prev / N + (1 - ALPHA) * np.maximum(post - 1.0, 0.0)
        g = np.maximum(xi / (1.0 + xi), FLOOR)
        self.prev = g * g * P
        act = bool(P[self.lowb].sum() / N[self.lowb].sum() > 4.0)
        # hiss tracker: 40th percentile in the log domain, slow while voice is about
        self.quiet_run = 0 if act else self.quiet_run + 1
        eta = ETA_QUIET if self.quiet_run > HOLD else ETA_VOICE
        self.logN += np.where(P > Nq, eta * PCT, -eta * (1.0 - PCT))
        self.acts.append(act)
        self.frames.append((S, g))
        if len(self.frames) <= LOOK:
            return None
        S0, g0 = self.frames.pop(0)
        a = np.array(self.acts[:2 * LOOK + 1])   # frames t-LOOK .. t+LOOK
        self.acts.pop(0)
        held = [a[j - HOLD:j + HOLD + 1].any() for j in range(LOOK - SOFT, LOOK + SOFT + 1)]
        wgt = sum(held) / float(len(held))
        G = (FLOOR + (g0 - FLOOR) * wgt) * self.band
        yf = np.fft.irfft(S0 * G, n=NFFT) * self.win
        self.obuf += yf
        out = self.obuf[:HOP] / 1.5              # periodic Hann^2 at hop N/4 sums to 1.5
        self.obuf = np.concatenate([self.obuf[HOP:], np.zeros(HOP)])
        return out

    def feed(self, raw):
        """raw: 1-D array of 12-bit ADC values. Returns float samples."""
        if len(raw) == 0:
            return np.zeros(0)
        x = self._dc(np.asarray(raw, float))
        x = np.concatenate([self.pend_in, x])
        nb = len(x) // HOP
        self.pend_in = x[nb * HOP:]
        outs = []
        for k in range(nb):
            y = self._notch(x[k * HOP:(k + 1) * HOP])
            o = self._frame(y)
            outs.append(o if o is not None else np.zeros(HOP))
        return np.concatenate(outs) if outs else np.zeros(0)


class SerialParser:
    """xiao_mic_uart.ino v2 framing: A = 1 c c h h h h h, B = 0 l l l l l l l.
    Lost samples (counter gap) are filled by holding the last value, as v9."""

    def __init__(self):
        self.carry = b""
        self.last_c = None
        self.last_v = 2048
        self.lost = 0

    def parse(self, chunk):
        b = np.frombuffer(self.carry + chunk, dtype=np.uint8)
        self.carry = b""
        if len(b) == 0:
            return np.zeros(0, dtype=np.int32)
        if b[-1] & 0x80:                       # A byte waiting for its B
            self.carry = bytes(b[-1:])
            b = b[:-1]
        isA = (b & 0x80) != 0
        idx = np.nonzero(isA[:-1] & ~isA[1:])[0]
        if len(idx) == 0:
            return np.zeros(0, dtype=np.int32)
        hi = b[idx].astype(np.int32)
        v = ((hi & 0x1F) << 7) | b[idx + 1].astype(np.int32)
        c = (hi >> 5) & 3
        prevc = np.concatenate([[c[0] - 1 if self.last_c is None else self.last_c], c[:-1]])
        gap = (c - prevc - 1) & 3
        if gap.any():
            self.lost += int(gap.sum())
            prevv = np.concatenate([[self.last_v], v[:-1]])
            # each sample is preceded by `gap` copies of the one before it
            v = np.repeat(np.stack([prevv, v], 1).ravel(),
                          np.stack([gap, np.ones_like(gap)], 1).ravel())
        self.last_c = int(c[-1])
        self.last_v = int(v[-1])
        return v


def open_serial(dev=DEV):
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY)
    a = termios.tcgetattr(fd)
    a[0] = 0
    a[1] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0
    a[4] = a[5] = termios.B1000000
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 2
    termios.tcsetattr(fd, termios.TCSANOW, a)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def probe_xiao(dev=DEV, secs=0.4, over=""):
    """13j: is a XIAO mic actually sending on the serial port? Returns
    (present, reason). Only listens on the PL011 UART with no serial console,
    so it never disturbs a Bluetooth UART or a login console on a kit robot."""
    import platform
    if platform.machine() == "armv6l" and over not in ("on", "1", "yes"):
        return False, "Pi Zero W (single core) is too slow for the mic filters"
    try:
        target = os.path.basename(os.path.realpath(dev))
    except OSError:
        target = ""
    if target != "ttyAMA0":
        return False, f"{dev} is {target or 'missing'}, not the main UART (needs dtoverlay=disable-bt)"
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read()
        if "console=serial0" in cmdline or "console=ttyAMA0" in cmdline:
            return False, "serial console is on the UART"
    except OSError:
        pass
    try:
        fd = open_serial(dev)
    except Exception as e:
        return False, f"can't open {dev}: {e}"
    par = SerialParser()
    n = 0
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < secs:
            time.sleep(0.05)
            n += len(par.parse(os.read(fd, 8192)))
    finally:
        os.close(fd)
    want = int(secs * RATE * 0.5)          # at least half the samples a XIAO sends
    if n >= want or over in ("on", "1", "yes"):
        return True, f"XIAO found ({n} samples in {secs} s)"
    return False, f"no mic data on {dev} ({n} samples in {secs} s)"


def _kernel():
    try:
        rel = os.uname().release.split("+")[0].split("-")[0]
        return tuple(int(x) for x in rel.split(".")[:3])
    except Exception:
        return (0, 0, 0)


def i2s_card():
    """13l: ALSA id of the I2S mic card (first of I2S_CARDS present), or None.
    /proc/asound/cards lines look like ' 0 [adau7002       ]: simple-card - ...'"""
    try:
        with open("/proc/asound/cards") as f:
            ids = re.findall(r"^\s*\d+\s+\[([^\]]+?)\s*\]", f.read(), re.M)
    except OSError:
        return None
    for name in I2S_CARDS:
        if name in ids:
            return name
    return None


def i2s_card_present():
    return i2s_card() is not None


def probe(dev=DEV, secs=0.4):
    """13k: which mic does this robot have? Returns (present, reason, source)
    with source "i2s", "xiao" or None. The I2S card is checked first (no
    recording needed - the sound card only exists if the overlay is on)."""
    over = ""
    try:
        with open(os.path.expanduser("~/.zerobot_mic")) as f:
            over = f.read().strip().lower()
    except OSError:
        pass
    if over in ("off", "0", "no"):
        return False, "switched off in ~/.zerobot_mic", None
    card = i2s_card() if over != "xiao" else None
    if card:
        k = _kernel()
        why = f"I2S mic (SPH0645, card {card})"
        if card in I2S_CARD_BAD_GPIO:
            why += (f" - NOTE this overlay holds GPIO {I2S_CARD_BAD_GPIO[card]} (RGB headlight "
                    "green): sudo bash ~/zerobot/setup-i2s-mic.sh && sudo reboot")
        if k < I2S_MIN_KERNEL:
            why += (f" - WARNING kernel {'.'.join(map(str, k))} is older than 6.18.60, "
                    "recordings may come back empty (sudo apt install linux-image-rpi-v7)")
        if not os.path.exists(ROOT_HELPER):
            why += " - WARNING root helper missing, the voice will break after LISTEN"
        return True, why, "i2s"
    if over == "i2s":
        return False, "~/.zerobot_mic says i2s but there is no I2S sound card", None
    ok, why = probe_xiao(dev, secs, over)
    return ok, why, ("xiao" if ok else None)


def _decimator(ntaps=63, cutoff=7000.0, fs=I2S_RATE):
    """Windowed-sinc low-pass for 48 -> 16 kHz."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(ntaps)
    return h / h.sum()


class I2SDSP:
    """48 kHz 32-bit stereo from arecord -> 16 kHz float in int16 units.
    Left channel only (SEL tied to GND). 18-bit data, left-justified."""

    def __init__(self):
        self.h = _decimator()
        try:
            from scipy.signal import butter, lfilter
            self._lfilter = lfilter
            self.hp_b, self.hp_a = butter(2, I2S_HP / (RATE / 2), "high")
        except Exception:
            self._lfilter = None
        self.reset()

    def reset(self):
        self.tail = np.zeros(len(self.h) - 1)  # FIR history
        self.phase = 0                         # which 48k sample is next to keep
        self.hp_zi = None
        self.hp_y = 0.0                        # fallback one-pole state
        self.hp_x = 0.0
        self.count = 0                         # 16k samples out since the start

    def feed(self, raw_bytes):
        if len(raw_bytes) < 8:
            return np.zeros(0)
        x = np.frombuffer(raw_bytes[: len(raw_bytes) // 8 * 8], dtype="<i4").reshape(-1, 2)[:, 0]
        x = (x >> 14).astype(float) / 4.0      # 18-bit -> int16 scale
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode="valid")   # one output per new input sample
        self.tail = buf[-(len(self.h) - 1):]
        y = y[self.phase::3]
        self.phase = (self.phase - len(x)) % 3
        if self._lfilter is not None:
            if self.hp_zi is None:
                from scipy.signal import lfilter_zi
                self.hp_zi = lfilter_zi(self.hp_b, self.hp_a) * (y[0] if len(y) else 0.0)
            y, self.hp_zi = self._lfilter(self.hp_b, self.hp_a, y, zi=self.hp_zi)
        else:                                  # one-pole high-pass, plain Python
            a = 1.0 / (1.0 + 2 * np.pi * I2S_HP / RATE)
            out = np.empty_like(y)
            px, py = self.hp_x, self.hp_y
            for i, v in enumerate(y):
                py = a * (py + v - px)
                px = v
                out[i] = py
            self.hp_x, self.hp_y = px, py
            y = out
        y = y * I2S_GAIN
        y = 32767.0 * np.tanh(y / 32767.0)    # soft limiter
        # silence while the mic settles after a start, then a short fade-in
        idx = self.count + np.arange(len(y))
        y *= np.clip((idx - I2S_SETTLE * RATE) / (I2S_FADE * RATE), 0.0, 1.0)
        self.count += len(y)
        return y


def _pcm(job):
    """Run the pigpio <-> mic hand-over as root (see pcm_swap.py)."""
    import subprocess
    try:
        r = subprocess.run(["sudo", "-n", ROOT_HELPER, "pcm", job],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


class MicLive:
    """One reader thread, any number of listeners. The thread runs only
    while at least one listener is subscribed."""

    def __init__(self, dev=DEV, is_muted=None):
        self.dev = dev
        self.is_muted = is_muted or (lambda: False)
        self.present = True          # 13j: robot.py sets this from probe()
        self.reason = ""
        self.lock = threading.Lock()
        self.subs = []
        self.thread = None
        self._stop = threading.Event()
        self.error = None
        self.lost = 0
        self.cpu_pct = 0.0
        self.muted = False
        self.learning = False
        self.phase = "off"           # 13p: off / talking (robot voice) / waking / live
        self.seconds = 0.0
        self.reads = 0
        self.dsp_s = 0.0
        # 13k: I2S mic
        self.source = "xiao"         # robot.py sets "i2s" from probe()
        self.hw_lock = threading.Lock()
        self._proc = None            # the running arecord
        self._cleared = False        # pigpio's PCM settings are saved + cleared
        self.handovers = 0

    def subscribe(self):
        q = queue.Queue(maxsize=40)              # ~2.5 s of 64 ms chunks
        with self.lock:
            self.subs.append(q)
            if self.thread is None or not self.thread.is_alive() or self._stop.is_set():
                old = self.thread
                self._stop = threading.Event()   # each reader thread has its own stop flag
                self.error = None
                self.thread = threading.Thread(target=self._run, args=(self._stop, old),
                                               daemon=True)
                self.thread.start()
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)
            if not self.subs:
                self._stop.set()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and not self._stop.is_set()

    def status(self):
        return {"available": self.present, "reason": self.reason, "source": self.source,
                "handovers": self.handovers, "listeners": len(self.subs), "running": self.running,
                "learning": self.learning, "muted": self.muted, "phase": self.phase if self.running else "off", "lost": self.lost,
                "cpu_pct": round(self.cpu_pct, 1), "seconds": round(self.seconds, 1),
                "error": self.error}

    def _send(self, pcm):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(pcm)
            except queue.Full:                   # a slow phone: drop its oldest chunk
                try:
                    q.get_nowait()
                    q.put_nowait(pcm)
                except Exception:
                    pass

    # ── 13k: I2S capture + hand-over with the voice ──────────────────────────
    def _start_capture(self):
        """Save + clear pigpio's PCM settings, then start arecord. Caller holds hw_lock."""
        import subprocess
        ok, msg = _pcm("save-clear")
        if not ok:
            raise RuntimeError(f"pigpio hand-over failed: {msg or 'root helper missing? sudo bash ~/zerobot/install-root-helper.sh'}")
        self._cleared = True
        card = i2s_card()
        if card is None:
            raise RuntimeError("no I2S sound card (adau7002 / sndrpigooglevoi) - boot config?")
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-D", f"hw:CARD={card}", "-c", "2", "-r", str(I2S_RATE),
             "-f", "S32_LE", "-t", "raw", "-B", "200000"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _stop_capture(self):
        """Stop arecord and give pigpio its PCM settings back. Safe to call any time."""
        with self.hw_lock:
            p, self._proc = self._proc, None
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=1.0)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
            if self._cleared:
                ok, msg = _pcm("restore")
                self._cleared = False
                self.handovers += 1
                if not ok:
                    self.error = f"couldn't give pigpio its settings back ({msg}) - restart: sudo systemctl restart pigpiod zerobot"
                    print(f"[MIC] {self.error}", flush=True)

    def pause_for_voice(self):
        """voice.py calls this just before its first wave (Voice.before_audio).
        Synchronous: when it returns, pigpio has its PCM settings back."""
        if self.source == "i2s" and (self._proc is not None or self._cleared):
            self._stop_capture()

    def _run(self, stop, old):
        if old is not None:
            old.join(2.0)                        # the last reader lets go first
        if self.source == "i2s":
            return self._run_i2s(stop)
        return self._run_xiao(stop)

    def _run_i2s(self, stop):
        dsp = I2SDSP()
        out = np.zeros(0)
        t0 = time.monotonic()
        tick_t, tick_c = t0, time.thread_time()
        mute_until = 0.0
        cap_t = 0.0                              # when this capture started
        cap_frames = 0                           # 48k frames received since then
        fails = 0
        self.learning = False
        try:
            while not stop.is_set():
                now = time.monotonic()
                talking = self.is_muted()
                if talking:
                    mute_until = now + I2S_UNMUTE_HANG
                self.muted = now < mute_until
                if self.muted:
                    self.phase = "talking" if talking else "waking"
                    if self._proc is not None or self._cleared:
                        self._stop_capture()     # normally already done by pause_for_voice
                    time.sleep(OUT_CHUNK / RATE)
                    self._send(bytes(2 * OUT_CHUNK))   # silence keeps the stream alive
                    continue
                if self._proc is None:
                    with self.hw_lock:
                        if self.is_muted():      # voice started meanwhile
                            continue
                        try:
                            self._start_capture()
                        except Exception as e:
                            self.error = str(e)
                            print(f"[MIC] {e}", flush=True)
                            stop.set()
                            break
                    dsp.reset()
                    out = np.zeros(0)
                    cap_t, cap_frames = time.monotonic(), 0
                    self.phase = "waking"
                p = self._proc
                if p is None:
                    continue
                try:
                    data = os.read(p.stdout.fileno(), 24576)   # up to 64 ms
                except (OSError, ValueError, AttributeError):
                    data = b""
                if not data:
                    if self._proc is p:                 # arecord died on its own
                        fails += 1
                        self.error = "arecord stopped unexpectedly"
                        self._stop_capture()
                        if fails >= 3:
                            print("[MIC] arecord keeps stopping - giving up", flush=True)
                            break
                        time.sleep(0.5)
                    continue
                cap_frames += len(data) // 8
                el = time.monotonic() - cap_t
                # 6.18.50 / pigpio clash symptom: data far faster than real time
                if el > 1.0 and cap_frames / I2S_RATE > el * 1.5 + 0.5:
                    self.error = ("mic hardware isn't clocked (recording runs faster than real time) - "
                                  "kernel older than 6.18.60, or pigpio still holds the PCM block")
                    print(f"[MIC] {self.error}", flush=True)
                    self._stop_capture()
                    break
                fails = 0
                d0 = time.thread_time()
                y = dsp.feed(data)
                self.dsp_s += time.thread_time() - d0
                self.phase = "live" if dsp.count >= (I2S_SETTLE + I2S_FADE) * RATE else "waking"
                self.reads += 1
                self.seconds = (cap_frames / I2S_RATE)
                out = np.concatenate([out, y])
                while len(out) >= OUT_CHUNK:
                    blk = np.clip(np.round(out[:OUT_CHUNK]), -32768, 32767)
                    out = out[OUT_CHUNK:]
                    self._send(blk.astype("<i2").tobytes())
                if now - tick_t >= 1.0:
                    c = time.thread_time()
                    self.cpu_pct = 100.0 * (c - tick_c) / (now - tick_t)
                    tick_t, tick_c = now, c
        except Exception as e:
            self.error = str(e)
            print(f"[MIC] {e}", flush=True)
        finally:
            stop.set()
            self._stop_capture()                 # ALWAYS give pigpio its settings back

    def _run_xiao(self, stop):
        fd = None
        try:
            fd = open_serial(self.dev)
        except Exception as e:
            self.error = f"can't open {self.dev}: {e}"
            stop.set()
            return
        dsp = MicDSP()
        par = SerialParser()
        out = np.zeros(0)
        t0 = time.monotonic()
        c0 = time.thread_time()
        tick_t, tick_c = t0, c0
        mute_until = 0.0
        was_muted = False
        got = 0
        try:
            while not stop.is_set():
                # 13g: the port hands back data as soon as ONE byte is there, so
                # reading flat out meant thousands of tiny reads a second and a
                # whole core spent on per-read overhead (13f: 99 % CPU). Nap
                # 25 ms between reads so each one gets ~800 bytes (the kernel
                # buffers 4 KB = 128 ms, so nothing is lost).
                time.sleep(READ_NAP)
                chunk = os.read(fd, 8192)
                if not chunk:
                    continue
                self.reads += 1
                raw = par.parse(chunk)
                got += len(raw)
                now = time.monotonic()
                if self.is_muted():
                    mute_until = now + UNMUTE_HANG
                muted = now < mute_until
                self.muted = muted
                self.phase = ("talking" if self.is_muted() else "waking") if muted else "live"
                if muted:
                    y = np.zeros(len(raw))           # skip the DSP: voice.py needs the CPU
                    was_muted = True
                else:
                    if was_muted:
                        dsp.reset()                  # stale buffers; keep the learned hiss
                        was_muted = False
                    d0 = time.thread_time()
                    y = dsp.feed(raw)
                    self.dsp_s += time.thread_time() - d0
                self.learning = dsp.logN is None
                out = np.concatenate([out, y])
                while len(out) >= OUT_CHUNK:
                    blk = np.clip(np.round(out[:OUT_CHUNK] * SCALE), -32768, 32767)
                    out = out[OUT_CHUNK:]
                    self._send(blk.astype("<i2").tobytes())
                self.lost = par.lost
                self.seconds = got / RATE
                if now - tick_t >= 1.0:
                    c = time.thread_time()
                    self.cpu_pct = 100.0 * (c - tick_c) / (now - tick_t)
                    tick_t, tick_c = now, c
        except Exception as e:
            self.error = str(e)
        finally:
            stop.set()
            try:
                os.close(fd)
            except Exception:
                pass


def _standalone(seconds):
    import wave
    path = os.path.expanduser("~/mic_live.wav")
    ok, why, src = probe()
    print(f"mic_live: {why}")
    if not ok:
        return
    mic = MicLive()
    mic.source = src
    q = mic.subscribe()
    if src == "i2s":
        print(f"mic_live: recording {seconds:.0f} s (I2S) - stop the robot's voice first; "
              "the first half second is silent while the mic settles")
    else:
        print(f"mic_live: recording {seconds:.0f} s - stay QUIET for the first second (learning the hiss)")
    data = bytearray()
    t0 = time.monotonic()
    cpu = []
    while time.monotonic() - t0 < seconds + 0.5:
        try:
            data += q.get(timeout=1.0)
        except queue.Empty:
            if mic.error:
                break
        if mic.cpu_pct:
            cpu.append(mic.cpu_pct)
    mic.unsubscribe(q)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(bytes(data))
    pcm = np.frombuffer(bytes(data), dtype="<i2") if data else np.zeros(1)
    print(f"saved {path}: {len(pcm) / RATE:.1f} s | lost {mic.lost} | peak {int(np.abs(pcm).max())} of 32767"
          f" | CPU (one core) avg {np.mean(cpu) if cpu else 0:.0f} % max {max(cpu) if cpu else 0:.0f} %"
          + (f" | ERROR {mic.error}" if mic.error else ""))
    secs = max(mic.seconds, 0.1)
    print(f"  breakdown: {mic.reads / secs:.0f} reads/s, {len(pcm) * 2 / max(mic.reads, 1):.0f} bytes/read,"
          f" filtering {100 * mic.dsp_s / secs:.0f} % of a core")


if __name__ == "__main__":
    _standalone(float(sys.argv[1]) if len(sys.argv) > 1 else 8.0)
