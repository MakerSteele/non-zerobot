"""ZeroBot carrier, Rev F: single source of truth for parts, placement (2D + height) and nets.
Coordinates in mm. Carrier origin = front-left corner (robot's left), X across the robot, Y front->rear.
Heights (z) are above the carrier's top copper. Every part has a plain-English name; ref designators are
only for KiCad and the silkscreen.

Rev F (11 Oct 2026): boost corner sockets become 1x2 with both pins bussed (strength). Nothing else moved.

Rev E (11 Oct 2026): Pi Zero 2 W unavailable, so the Pi (now a Pi 3) moves into the lid. The tall socket and
Pi standoffs are gone; a 2x10 header carries the 20 Pi pins we use, wired to the Pi with jumpers.

Rev C (6 Oct 2026), driven by Steven's ZerobotProV1 assembly:
- The Pi Zero plugs straight into a tall 2x20 socket on the carrier (pins down) and sits on 4 standoffs.
  No ribbon, no Pi adapter board. The lid is just a cover.
- Carrier sits on the battery-bay deck on 4 M2 screws into the deck's existing 1.6 mm holes, 3.5 mm spacers.
- Battery, switch and charger leads solder to pads. Charger stays on the front bulkhead.
- Headlight LEDs share one resistor per colour (both LEDs in parallel). 5 V rail capacitor dropped.
"""
import itertools

# ---------------- Board and enclosure ----------------
BOARD_W, BOARD_L = 41.8, 83.0          # battery-bay deck footprint (verified from assembly)
DECK_TO_PCB_BOTTOM = 3.5               # spacer height; deck top is 14.0 above the ring bottom
PCB_T = 1.6
# assembly mapping: asm X = 20.9 - x ; asm Z = -15.65 - y ; asm height = PCB_TOP_ASM + z
PCB_TOP_ASM = 14.0 + DECK_TO_PCB_BOTTOM + PCB_T     # 19.1
LID_MARGIN = 1.0

# Pi link (Rev E): the Pi 3 lives in the lid. The carrier has a 2x10 male pin header carrying only the
# 20 Pi pins we use; members wire each header pin to the Pi pin printed beside it (female-female jumpers).
LINK = [1, 2, 3, 4, 5, 6, 11, 13, 14, 15, 16, 18, 29, 31, 32, 33, 35, 36, 37, 39]   # header pin k -> Pi pin LINK[k-1]
LINK_X1, LINK_Y1 = 3.6, 30.0           # header pin 1 (odd pins at x, even pins at x + 2.54, 2.54 pitch toward the rear)
LINK_PIN_H = 8.5                       # plastic 2.5 + pin 6.0 above copper
LINK_PLUG_H = 22.0                     # top of the jumper housings plus the wire bend: the lid must clear this
def link_pad_xy(k):
    return LINK_X1 + (0 if k % 2 else 2.54), LINK_Y1 + 2.54 * ((k - 1) // 2)

# holes: (x, y, keepout radius, z-top of what sits there, name)
HOLES = [(x, y, 2.0, 1.5, "carrier screw (nylon M2 into deck)") for x, y in ((11.5, 9.0), (30.3, 9.0), (11.5, 42.1), (30.3, 42.1))]
SCREW_KEEPOUT = 1.7      # copper keep-out radius around the nylon carrier screws

P = {}
def part(ref, name, x, y, w, h, kind, conf="guess", short=None, note="", z=(0, 10), body=None, pins=None):
    """(x,y,w,h) = courtyard on the board, z = height range of that footprint.
    body = optional raised box (x, y, w, h, z0, z1) for a plug-in board that overhangs its socket."""
    P[ref] = dict(name=name, short=name if short is None else short, x=x, y=y, w=w, h=h, kind=kind, conf=conf,
                  note=note, z=z, body=body, pins=pins or [])

part("J1", "Pi link header (2x10)", LINK_X1 - 1.8, LINK_Y1 - 1.8, 6.15, 26.45, "conn", "common", "Pi link",
     "2x10 male pin header; jumper wires to the Pi pins printed beside it", z=(0, LINK_PIN_H),
     body=(LINK_X1 - 1.4, LINK_Y1 - 1.4, 5.35, 25.65, LINK_PIN_H - 6.0, LINK_PLUG_H))

TB_ROW_PITCH = 15.24     # confirmed by paper fit check (6 Oct 2026)
BOOST_W, BOOST_L = 15.8, 23.6          # measured from photo, +-0.3 (portrait: long side front-to-back)
BOOST_PAD_DX, BOOST_PAD_DY = 10.9, 18.7  # corner holes, centre to centre (measured)
BOOST_SOCKET_H = 8.5
# Rev F (Steven's idea): each boost corner is a 1x2 female header with both pins bussed together. The module's pin
# uses the outer hole (same place as Rev E); the second socket pin sits 2.54 mm INWARD in X, under the module, for
# strength. Inward (not front/back) because rear corners would run off the board edge and front corners would hit
# the headlight transistors. Left and right extras point opposite ways, so the module can't seat shifted by a pin.
BOOST_PAIR_DX = 2.54
# Both boosts are the same board in the same footprint orientation, so their trimmers sit on the SAME side.
# Photos 11 Oct 2026: VIN and OUT share one long edge (the carrier's left/+ edge), both GNDs on the other.
# The trimmer sits on the GND edge, so it lands on the RIGHT of each footprint. Only affects 3D / lid keep-out.
BOOST_TRIM_SIDE = "right"

# Front
part("J7", "Left headlight socket", 8.7, 0.2, 15.95, 6.8, "conn", "common", "Headlight L", "JST-XH 5-pin", z=(0, 13))
part("J8", "Right headlight socket", 34.7, 0.3, 6.8, 15.95, "conn", "common", "Headlight R", "JST-XH 5-pin", z=(0, 13))

def socket_rows(cx, cy, n, pitch_y):
    """two rows of n sockets along X, rows pitch_y apart, as (x0,y0,x1,y1,z0,z1) boxes"""
    w = (n - 1) * 2.54 + 2.6
    return [(cx - w / 2, cy + s_ * pitch_y / 2 - 1.3, cx + w / 2, cy + s_ * pitch_y / 2 + 1.3, 0, 8.5) for s_ in (-1, 1)]
# Motor driver: sockets on two rows, board raised 8.5-14.1; low parts may sit under it between the rows
_tx, _ty = 14.15 + 10.25, 10.6 + 10.25
part("U5", "Motor driver (TB6612)", 14.15, 10.6, 20.5, 20.5, "module", "verified", "Motor driver TB6612",
     "HW-166 on two 8-pin sockets (fit confirmed on paper)", z=(8.5, 14.1), pins=socket_rows(_tx, _ty, 8, TB_ROW_PITCH))
part("J2", "Charger pads", 20.0, 19.6, 8.65, 3.35, "pads", "common", "Charger", "under the motor driver, between its pin rows", z=(0, 4))
part("J5", "Left motor socket", 8.55, 12.0, 5.55, 14.95, "conn", "medium", "Motor L", "JST-PH 6-pin", z=(0, 10))
part("J6", "Right motor socket", 35.95, 17.0, 5.55, 14.95, "conn", "medium", "Motor R", "JST-PH 6-pin", z=(0, 10))
# Battery sense board (fit confirmed): socket on its left edge, board lies flat over the headlight resistors
part("U3", "Battery sense (ADS1115)", 14.6, 31.5, 3.6, 26.45, "module", "verified", "ADS1115",
     "CJMCU board on a 10-pin socket, lies flat over the resistors", z=(0, 8.5),
     body=(14.6, 31.2, 17.0, 28.0, 11.0, 13.6))
for i in range(8):
    c = ["white", "red", "green", "blue"][i % 4]
    nm = f"Transistor base resistor, {c}" if i < 4 else f"LED resistor, {c} (both LEDs)"
    part(f"R{i+1}", nm, 18.4, 32.6 + i * 3.25, 9.77, 3.05, "res", "common", "", z=(0, 3))
GROUPS = [dict(name="Headlight resistors (under the battery sense board)", x=18.4, y=32.4, w=10.2, h=26.4)]
part("F1", "Fuse", 9.5, 46.0, 3.55, 10.45, "fuse", "medium", "Fuse 4A", z=(0, 14))
# Right-hand strip
part("C2", "Motor rail capacitor", 34.6, 32.5, 6.85, 6.85, "cap", "common", "Motor cap", z=(0, 12))
part("C1", "Battery capacitor", 34.6, 39.6, 6.85, 6.85, "cap", "common", "Batt cap", z=(0, 12))
part("JP1", "Pi power jumper", 33.9, 47.0, 3.65, 6.2, "jumper", "common", "", z=(0, 11.5))
part("JP2", "Motor power jumper", 37.8, 47.0, 3.65, 6.2, "jumper", "common", "", z=(0, 11.5))
# Rear: two boosts side by side, long side front-to-back, on 4 corner sockets; trimmers toward the rear, clear of the Pi
def boost(ref, name, x0, short, trim_side=None):
    trim_side = trim_side or BOOST_TRIM_SIDE
    y0 = BOARD_L - BOOST_L
    cx, cy = x0 + BOOST_W / 2, y0 + BOOST_L / 2
    pins = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            px, py = cx + sx * BOOST_PAD_DX / 2, cy + sy * BOOST_PAD_DY / 2
            ix = px - sx * BOOST_PAIR_DX                     # inward partner pin
            pins.append((min(px, ix) - 1.3, py - 1.3, max(px, ix) + 1.3, py + 1.3, 0, BOOST_SOCKET_H))
    tx0 = x0 if trim_side == "left" else x0 + BOOST_W - 4.8
    part(ref, name, x0, y0, BOOST_W, BOOST_L, "module", "medium", short, "SX1308 board on 4 corner 1x2 sockets (pins bussed); pinout confirmed from photos",
         z=(BOOST_SOCKET_H, BOOST_SOCKET_H + 1.0 + 4.5), body=(tx0, y0 + 5.9, 4.8, 11.2, BOOST_SOCKET_H + 1.0, BOOST_SOCKET_H + 1.0 + 10.0), pins=pins)
boost("U1", "Pi power boost (set 5.1 V)", 8.2, "Pi boost 5.1V")
boost("U2", "Motor power boost (set 6.0 V)", 25.9, "Motor boost 6.0V")
# Under the boosts (low parts only, below the 8.5 mm boost boards)
for i in range(4):
    c = ["white", "red", "green", "blue"][i]
    part(f"Q{i+1}", f"Headlight transistor, {c}", 12.0 + i * 5.7, 63.8, 5.51, 4.79, "to92", "common", c[0].upper(), z=(0, 7.5))
part("R13", "Battery divider, top", 12.0, 69.0, 9.77, 3.05, "res", "common", "", z=(0, 3))
part("R14", "Battery divider, bottom", 22.4, 69.0, 9.77, 3.05, "res", "common", "", z=(0, 3))
part("C4", "Battery sense filter capacitor", 33.0, 69.0, 2.15, 4.65, "cap", "common", "", z=(0, 6))
# Left rear corner: battery and switch leads
part("J9", "Battery pads", 0.4, 64.0, 3.35, 8.65, "pads", "common", "Battery", z=(0, 4))
part("J10", "Power switch pads", 4.2, 64.0, 3.35, 8.65, "pads", "common", "Switch", z=(0, 4))



# ---------------- Signals ----------------
NET_DESC = {
    "5V": "5 V (Pi boost, after jumper)", "3V3": "3.3 V from the Pi", "GND": "Ground",
    "SDA": "I2C data", "SCL": "I2C clock",
    "AIN1": "Left motor forward", "AIN2": "Left motor backward",
    "BIN1": "Right motor forward", "BIN2": "Right motor backward",
    "ENC_L_H1": "Left encoder A", "ENC_L_H2": "Left encoder B",
    "ENC_R_H1": "Right encoder A", "ENC_R_H2": "Right encoder B",
    "HL_W": "Headlight white", "HL_R": "Headlight red", "HL_G": "Headlight green", "HL_B": "Headlight blue",
    "AO1": "Left motor +", "AO2": "Left motor −", "BO1": "Right motor +", "BO2": "Right motor −",
    "VM": "Motor power 6 V", "PACK": "Battery +", "PACK_F": "Battery + after fuse",
    "SW": "Switched battery +", "5V_SET": "Pi boost output (before jumper)",
    "VM_SET": "Motor boost output (before jumper)", "VSENSE": "Battery sense (half pack voltage)",
}
# physical Pi pin -> (net, BCM)
PI_PINS = {
    1: ("3V3", None), 2: ("5V", None), 4: ("5V", None),
    6: ("GND", None), 14: ("GND", None), 39: ("GND", None),
    3: ("SDA", 2), 5: ("SCL", 3),
    11: ("AIN1", 17), 13: ("AIN2", 27), 15: ("BIN1", 22), 16: ("BIN2", 23),
    29: ("ENC_L_H1", 5), 31: ("ENC_L_H2", 6), 33: ("ENC_R_H1", 13), 35: ("ENC_R_H2", 19),
    18: ("HL_W", 24), 32: ("HL_R", 12), 36: ("HL_G", 16), 37: ("HL_B", 26),
}
TB_LOGIC = ["PWMA", "AIN2", "AIN1", "STBY", "BIN1", "BIN2", "PWMB", "GND"]
TB_POWER = ["VM", "VCC", "GND", "AO1", "AO2", "BO2", "BO1", "GND"]
TB_NET = {"PWMA": "3V3", "AIN2": "AIN2", "AIN1": "AIN1", "STBY": "3V3", "BIN1": "BIN1", "BIN2": "BIN2",
          "PWMB": "3V3", "GND": "GND", "VM": "VM", "VCC": "3V3", "AO1": "AO1", "AO2": "AO2",
          "BO2": "BO2", "BO1": "BO1"}

PINS = {
    "J1": {k: PI_PINS[n][0] for k, n in enumerate(LINK, 1)},
    "U5": {f"{r}{i+1}:{n}": TB_NET[n] for r, row in (("L", TB_LOGIC), ("P", TB_POWER)) for i, n in enumerate(row)},
    "J5": {1: "GND", 2: "ENC_L_H1", 3: "ENC_L_H2", 4: "3V3", 5: "AO1", 6: "AO2"},
    "J6": {1: "GND", 2: "ENC_R_H1", 3: "ENC_R_H2", 4: "3V3", 5: "BO1", 6: "BO2"},
    "J7": {1: "5V", 2: "K_W", 3: "K_R", 4: "K_G", 5: "K_B"},
    "J8": {1: "5V", 2: "K_W", 3: "K_R", 4: "K_G", 5: "K_B"},
    "J9": {1: "PACK", 2: "GND"},
    "J10": {1: "PACK_F", 2: "SW"},
    "J2": {1: "PACK_F", 2: "GND"},
    "F1": {1: "PACK", 2: "PACK_F"},
    "U1": {"IN+": "SW", "IN-": "GND", "OUT+": "5V_SET", "OUT-": "GND"},
    "U2": {"IN+": "SW", "IN-": "GND", "OUT+": "VM_SET", "OUT-": "GND"},
    "JP1": {1: "5V_SET", 2: "5V"},
    "JP2": {1: "VM_SET", 2: "VM"},
    "U3": {"VDD": "3V3", "GND": "GND", "SCL": "SCL", "SDA": "SDA", "ADDR": "GND", "ALRT": "NC", "A0": "VSENSE",
           "A1": "NC", "A2": "NC", "A3": "NC"},
    "C1": {"+": "SW", "-": "GND"}, "C2": {"+": "VM", "-": "GND"},
    "C4": {1: "VSENSE", 2: "GND"},
    "R13": {1: "SW", 2: "VSENSE"}, "R14": {1: "VSENSE", 2: "GND"},
    "R1": {1: "HL_W", 2: "Q1B"}, "R2": {1: "HL_R", 2: "Q2B"}, "R3": {1: "HL_G", 2: "Q3B"}, "R4": {1: "HL_B", 2: "Q4B"},
    "Q1": {"E": "GND", "B": "Q1B", "C": "SINK_W"}, "Q2": {"E": "GND", "B": "Q2B", "C": "SINK_R"},
    "Q3": {"E": "GND", "B": "Q3B", "C": "SINK_G"}, "Q4": {"E": "GND", "B": "Q4B", "C": "SINK_B"},
    "R5": {1: "K_W", 2: "SINK_W"}, "R6": {1: "K_R", 2: "SINK_R"}, "R7": {1: "K_G", 2: "SINK_G"}, "R8": {1: "K_B", 2: "SINK_B"},
}

# ---------------- Checks ----------------
def boxes():
    """Every solid as (label, x0, y0, x1, y1, z0, z1)."""
    out = []
    for r, d in P.items():
        out.append((r, d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"], d["z"][0], d["z"][1]))
        if d["body"]:
            bx, by, bw, bh, z0, z1 = d["body"]; out.append((r + ":board", bx, by, bx + bw, by + bh, z0, z1))
        for k, (x0, y0, x1, y1, z0, z1) in enumerate(d["pins"]):
            out.append((f"{r}:pins{k}", x0, y0, x1, y1, z0, z1))
    for i, (hx, hy, rad, zt, nm) in enumerate(HOLES):
        out.append((f"hole{i}:{nm}", hx - rad, hy - rad, hx + rad, hy + rad, 0, zt))
    return out

def checks(ceiling=None):
    """ceiling(x0, y0, x1, y1) -> lowest lid underside above carrier copper over that box, or None."""
    out = []
    B = boxes()
    def owner(lbl): return lbl.split(":")[0]
    for a, b in itertools.combinations(B, 2):
        if owner(a[0]) == owner(b[0]): continue
        if a[0].startswith("hole") and b[0].startswith("hole"): continue
        xy = a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]
        for h, o in ((a, b), (b, a)):      # holes are round: use circle-to-box distance
            if h[0].startswith("hole") and xy:
                cx, cy, r = (h[1] + h[3]) / 2, (h[2] + h[4]) / 2, (h[3] - h[1]) / 2
                nx, ny = min(max(cx, o[1]), o[3]), min(max(cy, o[2]), o[4])
                xy = (nx - cx) ** 2 + (ny - cy) ** 2 < r * r
        zz = a[5] < b[6] and b[5] < a[6]
        if xy and zz:
            out.append(f"CLASH {a[0]} / {b[0]}")
    for r, d in P.items():
        if d["x"] < -0.01 or d["y"] < -0.01 or d["x"] + d["w"] > BOARD_W + 0.01 or d["y"] + d["h"] > BOARD_L + 0.01:
            out.append(f"OUT OF BOARD {r}")
    if ceiling is not None:
        for lbl, x0, y0, x1, y1, z0, z1 in B:
            if lbl.startswith("hole"): continue
            c = ceiling(x0, y0, x1, y1)
            if c is not None and z1 + LID_MARGIN > c:
                out.append(f"LID {lbl}: top {z1:.1f} vs lid {c:.1f} above carrier")
    nets = {}
    for ref, pins in PINS.items():
        for pin, net in pins.items():
            nets.setdefault(net, []).append(f"{ref}.{pin}")
    out += [f"SINGLE-NODE NET {n}" for n, v in nets.items() if n != "NC" and len(v) < 2]
    spec = {"AIN1": 17, "AIN2": 27, "BIN1": 22, "BIN2": 23, "ENC_L_H1": 5, "ENC_L_H2": 6,
            "ENC_R_H1": 13, "ENC_R_H2": 19, "SDA": 2, "SCL": 3, "HL_W": 24, "HL_R": 12, "HL_G": 16, "HL_B": 26}
    got = {v[0]: v[1] for v in PI_PINS.values() if v[1] is not None}
    out += [f"GPIO MISMATCH {k}" for k, v in spec.items() if got.get(k) != v]
    bcm_phys = {2: 3, 3: 5, 17: 11, 27: 13, 22: 15, 23: 16, 24: 18, 5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 16: 36, 26: 37}
    out += [f"PHYS MISMATCH GPIO{b}" for ph, (n, b) in PI_PINS.items() if b is not None and bcm_phys[b] != ph]
    area = sum(d["w"] * d["h"] for d in P.values())
    return out, nets, area

if __name__ == "__main__":
    problems, nets, area = checks()
    print("problems:", problems or "none")
    print(f"nets: {len([n for n in nets if n != 'NC'])}  parts: {len(P)}  fill: {area / (BOARD_W * BOARD_L) * 100:.0f}%")
