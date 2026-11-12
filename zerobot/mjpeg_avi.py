#!/usr/bin/env python3
"""Minimal MJPEG -> AVI muxer. Standard library only.

The camera already hands us finished JPEGs from the hardware encoder, so
recording costs no compression work at all - this just wraps those exact
bytes in a container that players will open. Writing raw concatenated JPEGs
would be cheaper still, but nothing plays the result.

AVI is used rather than MP4 because MJPEG in AVI needs no per-frame timing
metadata and the header can be patched at close with two seeks. An MP4 muxer
would be several hundred more lines for no benefit here.

Layout produced:

    RIFF 'AVI '
      LIST 'hdrl'
        'avih'                  main header
        LIST 'strl'
          'strh'                stream header
          'strf'                BITMAPINFOHEADER, compression 'MJPG'
      LIST 'movi'
        '00dc' <jpeg> ...       one chunk per frame, padded to even
      'idx1'                    index, required by most players to seek

Sizes and frame counts are unknown until the end, so they are written as
zero and patched in close().
"""

import os
import struct

_AVIF_HASINDEX = 0x00000010
_AVIIF_KEYFRAME = 0x00000010


def jpeg_size(buf):
    """Width/height from a JPEG's SOF marker, or None if it cannot be read.

    Used to make the header match the real frames rather than trusting a
    configured value that may have drifted from what the sensor is producing.
    """
    try:
        i = 2  # skip SOI
        n = len(buf)
        while i + 9 < n:
            if buf[i] != 0xFF:
                i += 1
                continue
            marker = buf[i + 1]
            # SOF0..SOF15, excluding DHT(C4), JPGA(C8) and DAC(CC)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = struct.unpack('>H', buf[i + 5:i + 7])[0]
                w = struct.unpack('>H', buf[i + 7:i + 9])[0]
                return w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack('>H', buf[i + 2:i + 4])[0]
            i += 2 + seg
    except Exception:
        pass
    return None


class AviWriter:
    """Append JPEG frames, then close() to finalise the header and index.

    Not thread-safe by itself - the recorder owns it from a single thread.
    """

    def __init__(self, path, width, height, fps=25.0):
        self.path = path
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps) if fps else 25.0
        self.frames = 0
        self.max_frame = 0
        self.bytes_written = 0
        self._index = []          # (offset_from_movi, length)
        self._dims_locked = False
        self._fh = open(path, 'wb')
        self._write_headers()

    # ── header ──────────────────────────────────────────────────────────
    def _write_headers(self):
        f = self._fh
        f.write(b'RIFF')
        self._riff_size_pos = f.tell()
        f.write(struct.pack('<I', 0))        # patched at close
        f.write(b'AVI ')

        f.write(b'LIST')
        f.write(struct.pack('<I', 4 + 8 + 56 + 4 + 8 + 8 + 56 + 8 + 40))
        f.write(b'hdrl')

        # ── avih ──
        f.write(b'avih')
        f.write(struct.pack('<I', 56))
        self._avih_pos = f.tell()
        f.write(struct.pack(
            '<14I',
            int(1_000_000 / self.fps),  # dwMicroSecPerFrame
            0,                          # dwMaxBytesPerSec (patched)
            0,                          # dwPaddingGranularity
            _AVIF_HASINDEX,             # dwFlags
            0,                          # dwTotalFrames (patched)
            0,                          # dwInitialFrames
            1,                          # dwStreams
            0,                          # dwSuggestedBufferSize (patched)
            self.width,
            self.height,
            0, 0, 0, 0))                # dwReserved[4]

        # ── strl ──
        f.write(b'LIST')
        f.write(struct.pack('<I', 4 + 8 + 56 + 8 + 40))
        f.write(b'strl')

        f.write(b'strh')
        f.write(struct.pack('<I', 56))
        self._strh_pos = f.tell()
        f.write(b'vids')
        f.write(b'MJPG')
        f.write(struct.pack('<I', 0))        # dwFlags
        f.write(struct.pack('<H', 0))        # wPriority
        f.write(struct.pack('<H', 0))        # wLanguage
        f.write(struct.pack('<I', 0))        # dwInitialFrames
        f.write(struct.pack('<I', 1000))     # dwScale
        f.write(struct.pack('<I', int(round(self.fps * 1000))))  # dwRate
        f.write(struct.pack('<I', 0))        # dwStart
        f.write(struct.pack('<I', 0))        # dwLength (patched)
        f.write(struct.pack('<I', 0))        # dwSuggestedBufferSize (patched)
        f.write(struct.pack('<i', -1))       # dwQuality (default)
        f.write(struct.pack('<I', 0))        # dwSampleSize
        f.write(struct.pack('<4h', 0, 0, self.width, self.height))  # rcFrame

        f.write(b'strf')
        f.write(struct.pack('<I', 40))
        self._strf_pos = f.tell()
        f.write(struct.pack('<I', 40))       # biSize
        f.write(struct.pack('<i', self.width))
        f.write(struct.pack('<i', self.height))
        f.write(struct.pack('<H', 1))        # biPlanes
        f.write(struct.pack('<H', 24))       # biBitCount
        f.write(b'MJPG')                     # biCompression
        f.write(struct.pack('<I', self.width * self.height * 3))  # biSizeImage
        f.write(struct.pack('<i', 0))        # biXPelsPerMeter
        f.write(struct.pack('<i', 0))        # biYPelsPerMeter
        f.write(struct.pack('<I', 0))        # biClrUsed
        f.write(struct.pack('<I', 0))        # biClrImportant

        # ── movi ──
        f.write(b'LIST')
        self._movi_size_pos = f.tell()
        f.write(struct.pack('<I', 0))        # patched at close
        self._movi_fourcc_pos = f.tell()     # index offsets are relative here
        f.write(b'movi')

    # ── frames ──────────────────────────────────────────────────────────
    def write_frame(self, jpeg):
        """Append one JPEG. Returns False if the frame was unusable."""
        if not jpeg:
            return False

        # Take the real dimensions from the first frame. If the sensor is
        # producing something other than the configured size, the header
        # should describe what is actually in the file.
        if not self._dims_locked:
            dims = jpeg_size(jpeg)
            if dims and dims != (self.width, self.height):
                self.width, self.height = dims
                self._patch_dims()
            self._dims_locked = True

        f = self._fh
        offset = f.tell() - self._movi_fourcc_pos
        n = len(jpeg)
        f.write(b'00dc')
        f.write(struct.pack('<I', n))
        f.write(jpeg)
        if n & 1:
            f.write(b'\x00')             # chunks must start on even offsets
        self._index.append((offset, n))
        self.frames += 1
        self.bytes_written += n
        if n > self.max_frame:
            self.max_frame = n
        return True

    def _patch_dims(self):
        f = self._fh
        here = f.tell()
        f.seek(self._avih_pos + 8 * 4)
        f.write(struct.pack('<II', self.width, self.height))
        f.seek(self._strh_pos + 48)
        f.write(struct.pack('<4h', 0, 0, self.width, self.height))
        f.seek(self._strf_pos + 4)
        f.write(struct.pack('<ii', self.width, self.height))
        f.seek(self._strf_pos + 20)
        f.write(struct.pack('<I', self.width * self.height * 3))
        f.seek(here)

    # ── close ───────────────────────────────────────────────────────────
    def close(self, actual_fps=None):
        """Write the index and patch every size field. Safe to call twice."""
        if self._fh is None:
            return
        f = self._fh
        try:
            movi_end = f.tell()
            movi_size = movi_end - self._movi_fourcc_pos

            # idx1
            f.write(b'idx1')
            f.write(struct.pack('<I', 16 * len(self._index)))
            for offset, length in self._index:
                f.write(b'00dc')
                f.write(struct.pack('<III', _AVIIF_KEYFRAME, offset, length))

            file_end = f.tell()

            # Playback speed: use the rate actually achieved, so a recording
            # made while the pipeline was struggling plays at real speed
            # rather than in fast motion.
            fps = actual_fps if actual_fps and actual_fps > 0 else self.fps
            per_sec = int(self.bytes_written * fps / self.frames) if self.frames else 0

            f.seek(self._riff_size_pos)
            f.write(struct.pack('<I', file_end - 8))

            f.seek(self._movi_size_pos)
            f.write(struct.pack('<I', movi_size))

            f.seek(self._avih_pos)
            f.write(struct.pack('<I', int(1_000_000 / fps)))
            f.seek(self._avih_pos + 4)
            f.write(struct.pack('<I', per_sec))
            f.seek(self._avih_pos + 16)
            f.write(struct.pack('<I', self.frames))
            f.seek(self._avih_pos + 28)
            f.write(struct.pack('<I', self.max_frame))

            f.seek(self._strh_pos + 20)          # dwScale
            f.write(struct.pack('<II', 1000, int(round(fps * 1000))))
            f.seek(self._strh_pos + 32)          # dwLength
            f.write(struct.pack('<II', self.frames, self.max_frame))

            f.flush()
            os.fsync(f.fileno())
        finally:
            try:
                f.close()
            finally:
                self._fh = None
