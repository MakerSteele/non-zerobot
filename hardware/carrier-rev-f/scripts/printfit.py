import sys, pcbnew, design as D
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, FancyBboxPatch
sys.path.insert(0, "kbuild"); from fpinfo import crt_bbox
b = pcbnew.LoadBoard("kicad/zerobot_carrier/zerobot_carrier.kicad_pcb")
PW, PH = 215.9, 279.4   # US Letter
W_IN, H_IN = PW / 25.4, PH / 25.4
fig = plt.figure(figsize=(W_IN, H_IN))
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, PW); ax.set_ylim(PH, 0); ax.axis("off")
ox, oy = 22, 42   # board origin on the page (mm)
def grid(x0, y0, w, h, label=True):
    import numpy as np
    for i in range(0, int(w) + 1):
        lw, c = (0.45, "#9aa5b1") if i % 10 == 0 else ((0.25, "#c3cbd3") if i % 5 == 0 else (0.12, "#e1e6ea"))
        ax.plot([x0 + i, x0 + i], [y0, y0 + h], color=c, lw=lw, zorder=0)
        if label and i % 10 == 0: ax.text(x0 + i, y0 - 1.2, str(i), fontsize=4.5, ha="center", color="#5d6d7e")
    for j in range(0, int(h) + 1):
        lw, c = (0.45, "#9aa5b1") if j % 10 == 0 else ((0.25, "#c3cbd3") if j % 5 == 0 else (0.12, "#e1e6ea"))
        ax.plot([x0, x0 + w], [y0 + j, y0 + j], color=c, lw=lw, zorder=0)
        if label and j % 10 == 0: ax.text(x0 - 1.2, y0 + j, str(j), fontsize=4.5, ha="right", va="center", color="#5d6d7e")
def fiducial(x, y, n):
    ax.add_patch(Rectangle((x - 3, y - 3), 6, 6, color="k", zorder=5)); ax.add_patch(Rectangle((x - 1.5, y - 1.5), 3, 3, color="w", zorder=6))
    ax.add_patch(Circle((x, y), 0.5, color="k", zorder=7)); ax.text(x + 4, y + 1, n, fontsize=5, zorder=7)
GX0, GY0, GW, GH = ox - 5, oy - 5, 52, 93      # grid area around the board, in board mm from (-5,-5)
grid(GX0, GY0, GW, GH)
for n, (fx, fy) in enumerate(((GX0 - 6, GY0 - 6), (GX0 + GW + 6, GY0 - 6), (GX0 - 6, GY0 + GH + 6), (GX0 + GW + 6, GY0 + GH + 6)), 1):
    fiducial(fx, fy, f"F{n}")
ax.text(PW / 2, 12, "ZeroBot carrier rev D  ·  1:1 fit check", ha="center", fontsize=14, weight="bold")
ax.text(PW / 2, 19, "Print at 100% / Actual size (not 'Fit to page'). A US quarter must sit exactly inside the circle, and the bar must measure 50 mm.", ha="center", fontsize=7.5)
ax.add_patch(Rectangle((PW - 72, 24), 50, 2.2, color="k")); ax.text(PW - 20, 25.1, "50 mm", fontsize=7, va="center")
ax.add_patch(FancyBboxPatch((ox, oy), D.BOARD_W, D.BOARD_L, boxstyle="round,pad=0,rounding_size=1", fill=False, lw=1.0))
for fp in b.GetFootprints():
    r = fp.GetReference()
    l, t, rr, bb = [v / 1e6 for v in crt_bbox(fp)]
    if not r.startswith("H"):
        ax.add_patch(Rectangle((ox + l, oy + t), rr - l, bb - t, fill=False, lw=0.4, ls=(0, (3, 2)), color="#555"))
    for p in fp.Pads():
        c = p.GetPosition(); x, y = ox + c.x / 1e6, oy + c.y / 1e6
        if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
            ax.add_patch(Circle((x, y), p.GetDrillSize().x / 2e6, fill=False, lw=0.8)); continue
        s = p.GetSize().x / 2e6
        if p.GetShape() == pcbnew.PAD_SHAPE_RECT: ax.add_patch(Rectangle((x - s, y - s), 2 * s, 2 * s, fill=False, lw=0.6))
        else: ax.add_patch(Circle((x, y), s, fill=False, lw=0.6))
        ax.add_patch(Circle((x, y), p.GetDrillSize().x / 2e6, color="k", lw=0))
# module outlines (guessed sizes) and the Pi
for ref in ("U1", "U2", "U5"):
    d = D.P[ref]; ax.add_patch(Rectangle((ox + d["x"], oy + d["y"]), d["w"], d["h"], fill=False, lw=1.2, color="#c0392b"))
bx, by, bw, bh, _, _ = D.P["U3"]["body"]; ax.add_patch(Rectangle((ox + bx, oy + by), bw, bh, fill=False, lw=1.2, color="#7d3c98"))
labels = {"U5": "MOTOR DRIVER", "U1": "PI BOOST", "U2": "MOTOR BOOST", "J7": "HEADLIGHT L", "J8": "HEAD R", "J5": "MOTOR L", "J6": "MOTOR R",
          "J9": "BATT", "J10": "SWITCH", "J2": "CHG", "F1": "FUSE", "C1": "470u", "C2": "220u", "J1": "PI LINK"}
for ref, txt in labels.items():
    d = D.P[ref]; rot = 90 if d["h"] > d["w"] * 1.5 else 0
    ax.text(ox + d["x"] + d["w"] / 2, oy + d["y"] + d["h"] / 2, txt, ha="center", va="center", fontsize=5.5, rotation=rot, color="#333")
ax.text(ox + bx + bw / 2, oy + by + bh / 2 + 6, "ADS1115\n(board outline)", ha="center", fontsize=5.5, color="#7d3c98")
# legend + steps
lx = GX0 + GW + 14; y = oy - 2
ax.text(lx, y, "How to use", fontsize=10, weight="bold"); y += 6
steps = ["1. Print at 100% / Actual size. Check the quarter",
         "   and the 50 mm bar. If off, reprint.",
         "2. Lay each real module on its RED outline, pins",
         "   over the black dots. Check rows, spacing and",
         "   pin 1 (square pad).",
         "3. ADS1115: purple outline, header on the 10 dots.",
         "4. Pi link header: a 2x10 pin header on its 20 dots.",
         "5. Hold a motor and headlight plug over their",
         "   sockets to check type and pin count.",
         "6. Photo each part in place, straight down, with",
         "   the 4 corner markers (F1-F4) in the shot.",
         "7. For a part that doesn't fit, lay it on the",
         "   measuring pad and photo it with F5-F8 in view.",
         "   I correct for camera angle from the markers.",
         "8. Also tell me each module's height (calipers),",
         "   board top to tallest part, with pins attached."]
for st in steps: ax.text(lx, y, st, fontsize=6.6); y += 4.3
y += 3
for col, ls, txt in (("#c0392b", "-", "module outline (my guess)"), ("#7d3c98", "-", "ADS1115 board outline"), ("#555", ":", "part keep-out area")):
    ax.plot([lx, lx + 8], [y, y], color=col, ls=ls, lw=1.2); ax.text(lx + 10, y + 1, txt, fontsize=7); y += 5
ax.text(GX0 + 2, GY0 + GH + 13, "Grid: 1 mm lines, numbers every 10 mm\nfrom the board front-left corner.\nFRONT of robot = top edge (headlights).\nTop view, parts side.", fontsize=6, va="top")
# quarter check circle (US quarter 24.26 mm)
qx, qy = 50, 222
ax.add_patch(Circle((qx, qy), 24.26 / 2, fill=False, lw=0.6, zorder=5))
ax.add_patch(Circle((qx, qy), 24.26 / 2 + 0.5, fill=False, lw=0.25, ls=":", zorder=5))
ax.text(qx, qy + 16.5, "US quarter (24.26 mm)\nshould just cover the solid ring", ha="center", fontsize=6)
# measuring pad for modules
MX0, MY0, MW, MH = PW - 112, 168, 60, 60
grid(MX0, MY0, MW, MH)
for n, (fx, fy) in enumerate(((MX0 - 6, MY0 - 6), (MX0 + MW + 6, MY0 - 6), (MX0 - 6, MY0 + MH + 6), (MX0 + MW + 6, MY0 + MH + 6)), 5):
    fiducial(fx, fy, f"F{n}")
ax.text(MX0, MY0 - 13, "MEASURING PAD: lay one module here, pins/header DOWN against the paper,\ncorner on the 0,0 corner. Photo straight down with all 4 markers in view.", fontsize=6)
fig.savefig("/mnt/user-data/outputs/zerobot_carrier_1to1_fitcheck.pdf")
print("ok")
