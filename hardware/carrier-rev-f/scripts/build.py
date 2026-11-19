"""Build KiCad 7 projects (schematic + PCB) for the ZeroBot carrier and Pi adapter from design.py.
Usage: python3 build.py   -> writes ../kicad/<project>/ ; routing/DRC/gerbers are separate steps (route.py)."""
import os, sys, json, uuid, math, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pcbnew
import design as D
from kicad_text import lib_symbol, dump, q
from fpinfo import crt_bbox

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kicad")
FPLIB = "/usr/share/kicad/footprints"
MM = 1e6
def mm(v): return int(round(v * MM))
def V(x, y): return pcbnew.VECTOR2I(mm(x), mm(y))
def uid(): return str(uuid.uuid4())

# ---------------------------------------------------------------- part catalogue
R_FP = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P2.54mm_Vertical"
CAT = {}   # ref -> dict(sym, fp, value, silk, pinmap{design pin key -> pad number})
def cat(ref, sym, fp, value, silk, pinmap=None):
    CAT[ref] = dict(sym=sym, fp=fp, value=value, silk=silk, pinmap=pinmap or {})

num = lambda n: {i: str(i) for i in range(1, n + 1)}
cat("J1", "Connector_Generic:Conn_02x10_Odd_Even", "Connector_PinHeader_2.54mm:PinHeader_2x10_P2.54mm_Vertical", "Pi link header 2x10", "PI PIN", num(20))
cat("U5", "Connector_Generic:Conn_01x16", "zerobot:TB6612_HW166_Socket", "TB6612 HW-166", "MOTOR DRIVER",
    {f"{r}{i+1}:{n}": str(i + 1 + (8 if r == "P" else 0)) for r, row in (("L", D.TB_LOGIC), ("P", D.TB_POWER)) for i, n in enumerate(row)})
for r, s_ in (("J5", "MOTOR L"), ("J6", "MOTOR R")):
    cat(r, "Connector_Generic:Conn_01x06", "Connector_JST:JST_PH_B6B-PH-K_1x06_P2.00mm_Vertical", D.P[r]["name"], s_, num(6))
for r, s_ in (("J7", "HEAD L"), ("J8", "HEAD R")):
    cat(r, "Connector_Generic:Conn_01x05", "Connector_JST:JST_XH_B5B-XH-A_1x05_P2.50mm_Vertical", D.P[r]["name"], s_, num(5))
WIRE = "Connector_Wire:SolderWire-0.5sqmm_1x02_P4.8mm_D0.9mm_OD2.3mm"
cat("J2", "Connector_Generic:Conn_01x02", WIRE, "Charger pads", "CHG + -", num(2))
cat("J9", "Connector_Generic:Conn_01x02", WIRE, "Battery pads", "BATT+  BATT-", num(2))
cat("J10", "Connector_Generic:Conn_01x02", WIRE, "Switch pads", "SWITCH", num(2))
cat("F1", "Device:Polyfuse", "Fuse:Fuse_Bourns_MF-RG400", "MF-RG400 (4 A)", "FUSE", num(2))
sx = {"IN+": "1", "IN-": "2", "OUT+": "3", "OUT-": "4"}
cat("U1", "Connector_Generic:Conn_01x04", "zerobot:SX1308_Module", "SX1308 set 5.1 V", "PI BOOST 5.1V", sx)
cat("U2", "Connector_Generic:Conn_01x04", "zerobot:SX1308_Module", "SX1308 set 6.0 V", "MOTOR BOOST 6.0V", sx)
cat("JP1", "Connector_Generic:Conn_01x02", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "Pi power jumper", "PI", num(2))
cat("JP2", "Connector_Generic:Conn_01x02", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "Motor power jumper", "MOT", num(2))
cat("U3", "Connector_Generic:Conn_01x10", "Connector_PinSocket_2.54mm:PinSocket_1x10_P2.54mm_Vertical", "ADS1115 (CJMCU)", "ADS",
    {"VDD": "1", "GND": "2", "SCL": "3", "SDA": "4", "ADDR": "5", "ALRT": "6", "A0": "7", "A1": "8", "A2": "9", "A3": "10"})
pol = {"+": "1", "-": "2"}
cat("C1", "Device:C_Polarized", "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm", "470u 6.3V", "470u", pol)
cat("C2", "Device:C_Polarized", "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm", "220u 10V", "220u", pol)
cat("C4", "Device:C", "Capacitor_THT:C_Disc_D3.0mm_W1.6mm_P2.50mm", "100n", "100n", num(2))
for i, col in enumerate("WRGB"):
    cat(f"Q{i+1}", "Transistor_BJT:PN2222A", "Package_TO_SOT_THT:TO-92_Inline", "PN2222A", col, {"E": "1", "B": "2", "C": "3"})
R_FLAT = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"
RV = {"R1": "1k", "R2": "1k", "R3": "1k", "R4": "1k", "R5": "39", "R6": "270", "R7": "180", "R8": "180"}
for r, v in RV.items():
    cat(r, "Device:R", R_FLAT, v, v, num(2))
for r in ("R13", "R14"):
    cat(r, "Device:R", R_FLAT, "10k 1%", "10k", num(2))
assert set(CAT) == set(D.PINS) == set(D.P) - set(), (set(CAT) ^ set(D.PINS))

# ---------------------------------------------------------------- custom footprints
def make_custom_lib(libdir):
    os.makedirs(libdir, exist_ok=True)
    io = pcbnew.IO_MGR.PluginFind(pcbnew.IO_MGR.KICAD_SEXP)
    def base(name, desc):
        fp = pcbnew.FOOTPRINT(None); fp.SetFPID(pcbnew.LIB_ID("zerobot", name)); fp.SetDescription(desc)
        fp.SetAttributes(pcbnew.FP_THROUGH_HOLE); return fp
    def rect(fp, layer, w, h, width):
        s = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_RECT); s.SetLayer(layer); s.SetWidth(mm(width))
        s.SetStart0(V(-w / 2, -h / 2)); s.SetEnd0(V(w / 2, h / 2)); fp.Add(s)
    def pth(fp, n, x, y, size=1.7, drill=1.0, square=False):
        p = pcbnew.PAD(fp); p.SetNumber(str(n)); p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        p.SetShape(pcbnew.PAD_SHAPE_RECT if square else pcbnew.PAD_SHAPE_CIRCLE)
        p.SetSize(V(size, size)); p.SetDrillSize(V(drill, drill)); p.SetLayerSet(p.PTHMask())
        p.SetPos0(V(x, y)); p.SetPosition(V(x, y)); fp.Add(p)
    # TB6612 HW-166: two 1x8 sockets, rows TB_ROW_PITCH apart. pads 1-8 logic row (top), 9-16 power row (bottom)
    fp = base("TB6612_HW166_Socket", "Generic TB6612FNG HW-166 module on two 1x8 female sockets. Row pitch is a GUESS.")
    rp = D.TB_ROW_PITCH / 2
    for i in range(8):
        pth(fp, i + 1, -8.89 + i * 2.54, -rp, square=(i == 0))
        pth(fp, i + 9, -8.89 + i * 2.54, rp)
    w = D.P["U5"]["w"]; rect(fp, pcbnew.F_SilkS, w - 0.5, w - 0.5, 0.12); rect(fp, pcbnew.F_Fab, w - 0.5, w - 0.5, 0.1)
    for sy in (-1, 1):   # courtyard only where the sockets stand; the module board is raised, low parts may sit under it
        s_ = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_RECT); s_.SetLayer(pcbnew.F_CrtYd); s_.SetWidth(mm(0.05))
        s_.SetStart0(V(-10.25, sy * rp - 1.6)); s_.SetEnd0(V(10.25, sy * rp + 1.6)); fp.Add(s_)
    io.FootprintSave(libdir, fp)
    # SX1308 module, measured: 15.8 x 23.6 board, corner holes 10.9 x 18.7 apart.
    # Rev F: each corner is a 1x2 female header; both pads share the pad number (bussed). The module pin uses the
    # outer hole; the partner pad sits BOOST_PAIR_DX inward in X. Pinout confirmed from photos 11 Oct 2026.
    fp = base("SX1308_Module", "SX1308 boost board (15.8 x 23.6) on 4 corner 1x2 sockets, pins bussed. VIN front-left, GND front-right, OUT rear-left, GND rear-right.")
    hx, hy = D.BOOST_PAD_DX / 2, D.BOOST_PAD_DY / 2
    for n, x, y in ((1, -hx, -hy), (2, hx, -hy), (3, -hx, hy), (4, hx, hy)):
        ix = x - (1 if x > 0 else -1) * D.BOOST_PAIR_DX
        pth(fp, n, x, y, 1.8, 1.0, square=(n == 1))
        pth(fp, n, ix, y, 1.8, 1.0)
        s_ = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_RECT); s_.SetLayer(pcbnew.F_CrtYd); s_.SetWidth(mm(0.05))
        s_.SetStart0(V(min(x, ix) - 1.4, y - 1.4)); s_.SetEnd0(V(max(x, ix) + 1.4, y + 1.4)); fp.Add(s_)
    rect(fp, pcbnew.F_SilkS, D.BOOST_W, D.BOOST_L, 0.12); rect(fp, pcbnew.F_Fab, D.BOOST_W, D.BOOST_L, 0.1)
    io.FootprintSave(libdir, fp)
    # Mounting slot 3.2 x 5.2 NPTH
    fp = base("Slot_3.2x5.2", "Mounting slot matching the printed carrier (3.2 wide, 5.2 long).")
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    p = pcbnew.PAD(fp); p.SetNumber(""); p.SetAttribute(pcbnew.PAD_ATTRIB_NPTH); p.SetShape(pcbnew.PAD_SHAPE_OVAL)
    p.SetSize(V(3.2, 5.2)); p.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_OBLONG); p.SetDrillSize(V(3.2, 5.2))
    p.SetLayerSet(p.UnplatedHoleMask()); fp.Add(p)
    c = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_CIRCLE); c.SetLayer(pcbnew.F_CrtYd); c.SetWidth(mm(0.05))
    c.SetStart0(V(0, 0)); c.SetEnd0(V(3.5, 0)); fp.Add(c)
    io.FootprintSave(libdir, fp)
    # Pi standoff hole (M2.5, 2.7 mm) with a 5 mm round standoff courtyard
    fp = base("Hole_M2.5_PiStandoff", "2.7 mm NPTH for an M2.5 x 16 mm standoff under the Pi Zero.")
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    p = pcbnew.PAD(fp); p.SetNumber(""); p.SetAttribute(pcbnew.PAD_ATTRIB_NPTH); p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    p.SetSize(V(2.7, 2.7)); p.SetDrillSize(V(2.7, 2.7)); p.SetLayerSet(p.UnplatedHoleMask()); fp.Add(p)
    for layer, r_, wd in ((pcbnew.F_CrtYd, 2.5, 0.05), (pcbnew.F_SilkS, 2.6, 0.12)):
        c = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_CIRCLE); c.SetLayer(layer); c.SetWidth(mm(wd)); c.SetStart0(V(0, 0)); c.SetEnd0(V(r_, 0)); fp.Add(c)
    io.FootprintSave(libdir, fp)
    # M2 screw hole with a tight courtyard (head 3.8 mm)
    fp = base("Hole_M2", "2.2 mm NPTH for an M2 screw into the battery-bay deck.")
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    p = pcbnew.PAD(fp); p.SetNumber(""); p.SetAttribute(pcbnew.PAD_ATTRIB_NPTH); p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    p.SetSize(V(2.2, 2.2)); p.SetDrillSize(V(2.2, 2.2)); p.SetLayerSet(p.UnplatedHoleMask()); fp.Add(p)
    c = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_CIRCLE); c.SetLayer(pcbnew.F_CrtYd); c.SetWidth(mm(0.05))
    c.SetStart0(V(0, 0)); c.SetEnd0(V(2.0, 0)); fp.Add(c)
    c = pcbnew.FP_SHAPE(fp, pcbnew.SHAPE_T_CIRCLE); c.SetLayer(pcbnew.F_SilkS); c.SetWidth(mm(0.12))
    c.SetStart0(V(0, 0)); c.SetEnd0(V(1.95, 0)); fp.Add(c)
    io.FootprintSave(libdir, fp)

def load_fp(fpid, libdir):
    lib, name = fpid.split(":")
    path = libdir if lib == "zerobot" else f"{FPLIB}/{lib}.pretty"
    fp = pcbnew.FootprintLoad(path, name)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp

# ---------------------------------------------------------------- placement helpers
def bbox_mm(fp):
    l, t, r, b = crt_bbox(fp); return l / MM, t / MM, r / MM, b / MM

def place_in_rect(fp, x, y, w, h, prefer=None):
    """Rotate so the courtyard's long axis matches the rect, then centre it in the rect."""
    best = None
    for rot in ([prefer] if prefer is not None else [0, 90]):
        fp.SetOrientationDegrees(rot)
        l, t, r, b = bbox_mm(fp)
        err = abs((r - l) - w) + abs((b - t) - h)
        if best is None or err < best[0]:
            best = (err, rot)
    fp.SetOrientationDegrees(best[1])
    l, t, r, b = bbox_mm(fp)
    pos = fp.GetPosition()
    dx, dy = (x + w / 2) - (l + r) / 2, (y + h / 2) - (t + b) / 2
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + mm(dx), pos.y + mm(dy)))

def add_text(board, txt, x, y, size=1.0, layer=pcbnew.F_SilkS, rot=0, bold=False):
    t = pcbnew.PCB_TEXT(board); t.SetText(txt); t.SetLayer(layer); t.SetPosition(V(x, y))
    t.SetTextSize(V(size, size)); t.SetTextThickness(mm(size * (0.18 if bold else 0.15)))
    if rot: t.SetTextAngleDegrees(rot)
    board.Add(t); return t

def outline(board, x0, y0, x1, y1, r=1.0):
    def seg(a, b):
        s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1)); s.SetStart(V(*a)); s.SetEnd(V(*b)); board.Add(s)
    def arc(c, start, ang):
        s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_ARC); s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1)); s.SetCenter(V(*c)); s.SetStart(V(*start)); s.SetArcAngleAndEnd(pcbnew.EDA_ANGLE(ang, pcbnew.DEGREES_T)); board.Add(s)
    seg((x0 + r, y0), (x1 - r, y0)); seg((x1, y0 + r), (x1, y1 - r))
    seg((x1 - r, y1), (x0 + r, y1)); seg((x0, y1 - r), (x0, y0 + r))
    arc((x1 - r, y0 + r), (x1 - r, y0), 90); arc((x1 - r, y1 - r), (x1, y1 - r), 90)
    arc((x0 + r, y1 - r), (x0 + r, y1), 90); arc((x0 + r, y0 + r), (x0, y0 + r), 90)

def gnd_zone(board, net, x0, y0, x1, y1, layer):
    z = pcbnew.ZONE(board); z.SetLayer(layer); z.SetNet(net); z.SetLocalClearance(mm(0.3))
    z.SetMinThickness(mm(0.25)); z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetThermalReliefGap(mm(0.4)); z.SetThermalReliefSpokeWidth(mm(0.5)); z.SetIsFilled(False)
    ol = z.Outline(); ol.NewOutline()
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ol.Append(mm(px), mm(py))
    board.Add(z)

# ---------------------------------------------------------------- project file
NETCLASS = {
    "Power": dict(track=1.0, nets=["GND", "PACK", "PACK_F", "SW"]),
    "MotorPower": dict(track=0.8, nets=["VM", "VM_SET", "5V", "5V_SET"]),
    "Motor": dict(track=0.6, nets=["AO1", "AO2", "BO1", "BO2"]),
}
def write_project(path, name):
    def cls(n, track, clr=0.2):
        return {"name": n, "clearance": clr, "track_width": track, "via_diameter": 0.8, "via_drill": 0.4,
                "microvia_diameter": 0.3, "microvia_drill": 0.1, "diff_pair_width": 0.2, "diff_pair_gap": 0.25,
                "diff_pair_via_gap": 0.25, "wire_width": 6, "bus_width": 12, "line_style": 0,
                "schematic_color": "rgba(0, 0, 0, 0.000)", "pcb_color": "rgba(0, 0, 0, 0.000)"}
    pro = {
        "board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.2, "min_via_diameter": 0.6,
                                                "min_through_hole_diameter": 0.3, "min_hole_to_hole": 0.25,
                                                "min_copper_edge_clearance": 0.2, "min_text_height": 0.7, "min_silk_clearance": 0.0},
                                "rule_severities": {"starved_thermal": "warning", "lib_footprint_issues": "ignore",
                                                    "lib_footprint_mismatch": "ignore", "silk_overlap": "warning"}}},
        "meta": {"filename": f"{name}.kicad_pro", "version": 1},
        "net_settings": {"classes": [cls("Default", 0.3)] + [cls(k, v["track"]) for k, v in NETCLASS.items()],
                         "meta": {"version": 3},
                         "netclass_patterns": [{"netclass": k, "pattern": n} for k, v in NETCLASS.items() for n in v["nets"]]},
        "pcbnew": {"page_layout_descr_file": ""},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [], "text_variables": {},
    }
    json.dump(pro, open(path, "w"), indent=2)

# ---------------------------------------------------------------- schematic writer
def write_schematic(path, project, title, parts, nets_of, groups, notes):
    """parts: list of (ref, sym, value, fp, x, y). nets_of(ref, pin) -> net name or 'NC'.
    Labels sit directly on pin ends so connectivity is by name."""
    root = uid()
    syms, body, done = [], [], set()
    for ref, sym, value, fp, x, y in parts:
        s, pins = lib_symbol(sym)
        if sym not in done:
            syms.append(s); done.add(sym)
        el = ["symbol", ["lib_id", q(sym)], ["at", f"{x:.2f}", f"{y:.2f}", "0"], ["unit", "1"],
              ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"], ["uuid", uid()]]
        def prop(k, v, dx, dy, hide=False):
            eff = ["effects", ["font", ["size", "1.27", "1.27"]]]
            if hide: eff.append("hide")
            return ["property", q(k), q(v), ["at", f"{x+dx:.2f}", f"{y+dy:.2f}", "0"], eff]
        top = max(p[2] for p in pins) + 2.54
        bot = -min(p[2] for p in pins) + 3.81
        el += [prop("Reference", ref, 0, -top - 0.5), prop("Value", value, 0, bot),
               prop("Footprint", fp, 0, 0, True), prop("Datasheet", "~", 0, 0, True)]
        for p in pins:
            el.append(["pin", q(p[0]), ["uuid", uid()]])
        el.append(["instances", ["project", q(project), ["path", q("/" + root), ["reference", q(ref)], ["unit", "1"]]]])
        body.append(el)
        for pnum, px, py, ang in pins:
            ax, ay = x + px, y - py
            net = nets_of(ref, pnum)
            if net == "NC":
                body.append(["no_connect", ["at", f"{ax:.2f}", f"{ay:.2f}"], ["uuid", uid()]])
                continue
            la = {0: 180, 180: 0, 90: 270, 270: 90}[ang]
            just = {0: ["justify", "left", "bottom"], 180: ["justify", "right", "bottom"],
                    90: ["justify", "left", "bottom"], 270: ["justify", "right", "bottom"]}[la]
            body.append(["label", q(net), ["at", f"{ax:.2f}", f"{ay:.2f}", str(la)], ["fields_autoplaced"],
                         ["effects", ["font", ["size", "1.0", "1.0"]], just], ["uuid", uid()]])
    for gx, gy, txt in groups + notes:
        body.append(["text", q(txt), ["at", f"{gx:.2f}", f"{gy:.2f}", "0"],
                     ["effects", ["font", ["size", "2.0" if (gx, gy, txt) in groups else "1.27", "2.0" if (gx, gy, txt) in groups else "1.27"]], ["justify", "left", "bottom"]], ["uuid", uid()]])
    doc = ["kicad_sch", ["version", "20230121"], ["generator", "eeschema"], ["uuid", root], ["paper", "A3"],
           ["title_block", ["title", q(title)], ["date", "2026-10-11"], ["rev", q("E")], ["company", q("ZeroBot kit")],
            ["comment", "1", q("Generated from zerobot_carrier_design.py - module sizes marked GUESS in the design page")]],
           ["lib_symbols"] + syms] + body + [["sheet_instances", ["path", q("/"), ["page", q("1")]]]]
    open(path, "w").write(dump(doc) + "\n")

def fp_lib_table(path):
    open(path, "w").write('(fp_lib_table\n  (version 7)\n  (lib (name "zerobot")(type "KiCad")(uri "${KIPRJMOD}/zerobot.pretty")(options "")(descr "ZeroBot custom footprints"))\n)\n')

# ================================================================= CARRIER
def build_carrier():
    name = "zerobot_carrier"; d = os.path.join(OUT, name)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    lib = os.path.join(d, "zerobot.pretty"); make_custom_lib(lib); fp_lib_table(os.path.join(d, "fp-lib-table"))
    board = pcbnew.BOARD()
    board.GetDesignSettings().SetCopperLayerCount(2)
    nets = {}
    def net(n):
        if n not in nets:
            ni = pcbnew.NETINFO_ITEM(board, n); board.Add(ni); nets[n] = ni
        return nets[n]
    for n in sorted({v for p in D.PINS.values() for v in p.values() if v != "NC"}):
        net(n)
    padnet = {}
    for ref, c in CAT.items():
        d_ = D.P[ref]
        fp = load_fp(c["fp"], lib)
        fp.SetReference(ref); fp.SetValue(c["value"])
        fp.Reference().SetLayer(pcbnew.F_Fab); fp.Value().SetLayer(pcbnew.F_Fab); fp.Value().SetVisible(False)
        fp.Reference().SetTextSize(V(0.8, 0.8)); fp.Reference().SetTextThickness(mm(0.12))
        board.Add(fp)
        if ref == "J1":
            fp.SetOrientationDegrees(0); fp.SetPosition(V(*D.link_pad_xy(1)))
        elif ref in ("U1", "U2", "U5"):
            fp.SetOrientationDegrees(0); fp.SetPosition(V(d_["x"] + d_["w"] / 2, d_["y"] + d_["h"] / 2))
        else:
            place_in_rect(fp, d_["x"], d_["y"], d_["w"], d_["h"])
        for key, n in D.PINS[ref].items():
            padnum = c["pinmap"][key]
            padnet[(ref, padnum)] = n
            for p in fp.Pads():
                if p.GetNumber() == padnum and n != "NC":
                    p.SetNet(net(n))
        fp.SetLocked(False)
    # holes
    for i, (hx, hy, rad, zt, nm) in enumerate(D.HOLES):
        fpid = "zerobot:Hole_M2.5_PiStandoff" if "Pi standoff" in nm else "zerobot:Hole_M2"
        fp = load_fp(fpid, lib); fp.SetReference(f"H{i+1}"); fp.SetValue(nm)
        fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
        board.Add(fp); fp.SetPosition(V(hx, hy))
    outline(board, 0, 0, D.BOARD_W, D.BOARD_L, 1.0)
    for hx, hy, rad, zt, nm in D.HOLES:     # no tracks or vias under screw heads; ground pour is allowed
        rad = D.SCREW_KEEPOUT if "screw" in nm else rad
        z = pcbnew.ZONE(board); z.SetIsRuleArea(True); z.SetDoNotAllowTracks(True); z.SetDoNotAllowVias(True)
        z.SetDoNotAllowCopperPour(False); z.SetDoNotAllowPads(False); z.SetDoNotAllowFootprints(False)
        ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu); z.SetLayerSet(ls)
        z.SetZoneName(f"keepout {nm}")
        ol = z.Outline(); ol.NewOutline()
        for k in range(16):
            a = 2 * math.pi * k / 16
            ol.Append(mm(hx + rad * math.cos(a)), mm(hy + rad * math.sin(a)))
        board.Add(z)
    # silkscreen: plain-language labels, placed in free space near each part
    silk(board, {r: c["silk"] for r, c in CAT.items()})
    for k, n in enumerate(D.LINK, 1):      # Pi pin number beside every header pin: wire pin "29" to Pi pin 29
        x, y = D.link_pad_xy(k)
        add_text(board, str(n), x - 2.5 if k % 2 else x + 2.35, y, 0.8, bold=True)
    add_text(board, "ZEROBOT CARRIER F", 3.2, 77.8, 0.8, rot=90, bold=True)
    path = os.path.join(d, f"{name}.kicad_pcb")
    board.Save(path)
    write_project(os.path.join(d, f"{name}.kicad_pro"), name)
    # schematic
    layout = sch_layout_carrier()
    parts = [(ref, CAT[ref]["sym"], CAT[ref]["value"], CAT[ref]["fp"], x, y) for ref, (x, y) in layout.items()]
    inv = {ref: {pad: n for (r, pad), n in padnet.items() if r == ref} for ref in CAT}
    write_schematic(os.path.join(d, f"{name}.kicad_sch"), name, "ZeroBot carrier board", parts,
                    lambda ref, pin: inv[ref].get(pin, "NC"), SCH_GROUPS, SCH_NOTES)
    return d, padnet

SCH_GROUPS, SCH_NOTES = [], []
def sch_layout_carrier():
    """Grid positions (mm, snapped to 2.54) for symbols on A3, grouped by function."""
    g = lambda v: round(v / 2.54) * 2.54
    L = {}
    SCH_GROUPS.clear(); SCH_NOTES.clear()
    # power entry
    SCH_GROUPS.append((20, 25, "POWER ENTRY"))
    for i, r in enumerate(["J9", "F1", "J10", "J2", "C1"]):
        L[r] = (g(30 + i * 22), g(45))
    SCH_GROUPS.append((20, 70, "BOOSTS + SAFETY JUMPERS (fit jumper only after metering the boost)"))
    for i, r in enumerate(["U1", "JP1", "U2", "JP2", "C2"]):
        L[r] = (g(30 + i * 22), g(90))
    SCH_GROUPS.append((20, 115, "MOTOR DRIVER + MOTORS"))
    for i, r in enumerate(["U5", "J5", "J6"]):
        L[r] = (g(35 + i * 30), g(145))
    SCH_GROUPS.append((165, 25, "PI LINK HEADER (jumper each pin to the Pi pin named on its label)"))
    L["J1"] = (g(200), g(62))
    SCH_GROUPS.append((260, 115, "BATTERY SENSE"))
    for i, r in enumerate(["U3", "R13", "R14", "C4"]):
        L[r] = (g(270 + i * 20), g(145))
    SCH_GROUPS.append((20, 185, "HEADLIGHT DRIVER  (Base kit: Q1 R1 R5.  Plus RGB adds Q2-Q4 R2-R4 R6-R8)"))
    for i, r in enumerate(["J7", "J8"]):
        L[r] = (g(30 + i * 25), g(215))
    for i, r in enumerate(["Q1", "Q2", "Q3", "Q4"]):
        L[r] = (g(90 + i * 22), g(205))
    for i, r in enumerate(["R1", "R2", "R3", "R4"]):
        L[r] = (g(90 + i * 22), g(235))
    for i, r in enumerate(["R5", "R6", "R7", "R8"]):
        L[r] = (g(190 + i * 18), g(220))
    SCH_NOTES.append((20, 280, "Labels on pins connect by name. Module pinouts (TB6612 rows, SX1308 pads, ADS1115 order) are best guesses - see design page."))
    return L

OWN_SILK_OK = {"J7", "J8", "J9", "J10", "Q1", "Q2", "Q3", "Q4"}
SILK_POS = {f"Q{i+1}": (12.0 + i * 5.7 + 2.75, 68.75, 0, 0.7) for i in range(4)}
SILK_POS["J1"] = (D.LINK_X1 + 0.5, D.LINK_Y1 - 2.6, 0, 0.7)
SILK_POS["R13"] = (16.9, 72.7, 0, 0.8)   # hand-placed labels for parts that sit under the boosts
def silk(board, labels):
    """Put each label in the first free spot: inside its own outline clear of pads, else just outside it."""
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    pads = []
    for f in fps.values():
        for p in f.Pads():
            b = p.GetBoundingBox(); pads.append((b.GetLeft()/MM-0.2, b.GetTop()/MM-0.2, b.GetRight()/MM+0.2, b.GetBottom()/MM+0.2))
    taken = []
    def hits(box, rects):
        x0, y0, x1, y1 = box
        return any(x0 < r and l < x1 and y0 < b and t < y1 for l, t, r, b in rects)
    def free(box, own):
        x0, y0, x1, y1 = box
        if x0 < 0.3 or y0 < 0.3 or x1 > D.BOARD_W - 0.3 or y1 > D.BOARD_L - 0.3: return False
        others = [bbox_mm(f) for r, f in fps.items() if r != own]
        ownsilk = []
        if fps[own].GetFPID().GetLibNickname() != "zerobot" and own not in OWN_SILK_OK:
            for g in fps[own].GraphicalItems():
                if g.GetLayer() == pcbnew.F_SilkS:
                    gb = g.GetBoundingBox(); ownsilk.append((gb.GetLeft()/MM-0.1, gb.GetTop()/MM-0.1, gb.GetRight()/MM+0.1, gb.GetBottom()/MM+0.1))
        if hits(box, others) or hits(box, pads) or hits(box, taken) or hits(box, ownsilk): return False
        for hx, hy, rad, _z, _ in D.HOLES:
            cx, cy = min(max(hx, x0), x1), min(max(hy, y0), y1)
            if (cx - hx) ** 2 + (cy - hy) ** 2 < (rad + 0.2) ** 2: return False
        return True
    for ref, txt in labels.items():
        if ref in SILK_POS:
            x, y, rot, size = SILK_POS[ref]; add_text(board, txt, x, y, size, rot=rot); continue
        l, t, r, b = bbox_mm(fps[ref])
        for size in (0.9, 0.8):
            tw, th = len(txt) * size * 0.72 + 0.2, size + 0.2
            placed = False
            for rot in (0, 90):
                w_, h_ = (tw, th) if rot == 0 else (th, tw)
                cx, cy = (l + r) / 2, (t + b) / 2
                cands = []
                inside_ok = True
                for fy in (0.5, 0.25, 0.75, 0.12, 0.88):
                    for fx in (0.5, 0.3, 0.7):
                        if inside_ok: cands.append((l + (r - l) * fx, t + (b - t) * fy))
                cands += [(cx, t - h_ / 2 - 0.05), (cx, b + h_ / 2 + 0.05), (l - w_ / 2 - 0.05, cy), (r + w_ / 2 + 0.05, cy)]
                for x, y in cands:
                    box = (x - w_ / 2, y - h_ / 2, x + w_ / 2, y + h_ / 2)
                    if free(box, ref):
                        add_text(board, txt, x, y, size, rot=rot); taken.append(box); placed = True; break
                if placed: break
            if placed: break
        else:
            print("  silk: no room for", ref, txt, "- left on fab layer only")

# ================================================================= PI ADAPTER
def build_adapter():
    name = "zerobot_pi_adapter"; d = os.path.join(OUT, name)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    board = pcbnew.BOARD(); board.GetDesignSettings().SetCopperLayerCount(2)
    nets = {}
    def net(n):
        if n not in nets:
            ni = pcbnew.NETINFO_ITEM(board, n); board.Add(ni); nets[n] = ni
        return nets[n]
    padnet = {}
    # J11: 2x20 socket on the underside; pad n must land on Pi pin n (Pi frame: pin1 at 8.37,4.77; even row at y=2.23)
    j11 = load_fp("Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical", None)
    j11.SetReference("J11"); j11.SetValue("Pi header socket"); board.Add(j11)
    def target(n): return 8.37 + 2.54 * ((n - 1) // 2), (4.77 if n % 2 else 2.23)
    ok = False
    for flip in (True,):
        for rot in (0, 90, 180, 270):
            j11.SetOrientationDegrees(0); j11.SetPosition(V(0, 0))
            if j11.IsFlipped() != flip: j11.Flip(j11.GetPosition(), False)
            j11.SetOrientationDegrees(rot)
            pads = {p.GetNumber(): p for p in j11.Pads()}
            p1 = pads["1"].GetPosition(); tx, ty = target(1)
            j11.SetPosition(pcbnew.VECTOR2I(j11.GetPosition().x + mm(tx) - p1.x, j11.GetPosition().y + mm(ty) - p1.y))
            err = max(abs(pads[str(n)].GetPosition().x / MM - target(n)[0]) + abs(pads[str(n)].GetPosition().y / MM - target(n)[1]) for n in range(1, 41))
            if err < 0.01: ok = True; break
        if ok: break
    assert ok, "could not align J11 to Pi header"
    j11.Reference().SetLayer(pcbnew.B_Fab); j11.Value().SetVisible(False)
    for n in range(1, 41):
        if n in D.PI_PINS:
            netname = D.PI_PINS[n][0]
            for p in j11.Pads():
                if p.GetNumber() == str(n): p.SetNet(net(netname))
            padnet[("J11", str(n))] = netname
        else:
            padnet[("J11", str(n))] = "NC"
    # J12 box header on top, horizontal, pin 1 at left
    j12 = load_fp("Connector_IDC:IDC-Header_2x10_P2.54mm_Vertical", None)
    j12.SetReference("J12"); j12.SetValue("Ribbon socket"); board.Add(j12)
    a = D.ADP_PARTS["J12"]
    place_in_rect(j12, a["x"], a["y"] + 0.4, 34.11, 9.95, prefer=270)
    j12.Reference().SetLayer(pcbnew.F_Fab); j12.Value().SetVisible(False)
    for p in j12.Pads():
        n = D.CABLE[int(p.GetNumber())]; p.SetNet(net(n)); padnet[("J12", p.GetNumber())] = n
    A = D.ADP
    outline(board, A["x"], A["y"], A["x"] + A["w"], A["y"] + A["h"], 1.0)
    add_text(board, "ZeroBot Pi adapter  rev B", 32.5, 21.0, 0.9, bold=True)
    add_text(board, "PIN 1", 8.6, 7.6, 0.8)
    add_text(board, "PIN 1", 8.6, 7.6, 0.8, layer=pcbnew.B_SilkS).SetMirrored(True)
    add_text(board, "TO CARRIER", 53.5, 14.5, 0.8, rot=90)
    path = os.path.join(d, f"{name}.kicad_pcb"); board.Save(path)
    write_project(os.path.join(d, f"{name}.kicad_pro"), name)
    SCH_GROUPS.clear(); SCH_NOTES.clear()
    SCH_GROUPS.append((20, 25, "PI ADAPTER: Pi header socket (underside) -> 20-way ribbon to carrier"))
    SCH_NOTES.append((20, 150, "Pins not listed are unused. 3.3 V pins 1 and 17 are both connected; all 8 Pi grounds are tied together."))
    parts = [("J11", "Connector_Generic:Conn_02x20_Odd_Even", "Pi header socket", "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical", 60.96, 88.9),
             ("J12", "Connector_Generic:Conn_02x10_Odd_Even", "Ribbon socket", "Connector_IDC:IDC-Header_2x10_P2.54mm_Vertical", 180.34, 76.2)]
    write_schematic(os.path.join(d, f"{name}.kicad_sch"), name, "ZeroBot Pi adapter", parts,
                    lambda ref, pin: padnet.get((ref, pin), "NC"), SCH_GROUPS, SCH_NOTES)
    return d, padnet

if __name__ == "__main__":
    shutil.rmtree(os.path.join(OUT, "zerobot_pi_adapter"), ignore_errors=True)   # rev C: no adapter board
    cd, cpn = build_carrier(); print("carrier ->", cd)
    json.dump({"carrier": {f"{r}.{p}": n for (r, p), n in cpn.items()}}, open(os.path.join(OUT, "expected_nets.json"), "w"), indent=1)
