#!/usr/bin/env python3
"""Single source of truth for the build identity.

Imported by robot.py (control, :5000) and camera.py (stream, :8080). Both
report it, and the page reports the copy that was baked into it when Flask
rendered it. Three answers to the same question - see the UI's VER chip.

BUMP `BUILD` EVERY TIME THE ZIP CHANGES. A version that does not move cannot
tell you that the old code is still running, which is the entire point.
"""

VERSION = "4.7"
BUILD   = "2026-11-18x"
STAMP   = f"{VERSION}+{BUILD}"
