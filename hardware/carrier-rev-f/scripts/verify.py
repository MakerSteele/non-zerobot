"""Independent checks on the generated KiCad carrier (rev E). Prints PASS/FAIL; exit 1 on any FAIL."""
import os, sys, json, subprocess, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pcbnew
import design as D
from kicad_text import parse

K = os.path.join(ROOT, "kicad"); proj = "zerobot_carrier"
exp = json.load(open(os.path.join(K, "expected_nets.json")))["carrier"]
fails, warns = [], []
def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok: fails.append(msg)

# schematic netlist
sch = os.path.join(K, proj, f"{proj}.kicad_sch"); out = os.path.join(K, proj, "sch_netlist.net")
subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, sch], capture_output=True)
s = {}
for sec in parse(open(out).read())[0]:
    if isinstance(sec, list) and sec[0] == "nets":
        for n in sec[1:]:
            name = next(x for x in n if isinstance(x, list) and x[0] == "name")[1].strip('"').lstrip("/")
            for node in n:
                if isinstance(node, list) and node[0] == "node":
                    ref = next(x for x in node if isinstance(x, list) and x[0] == "ref")[1].strip('"')
                    pin = next(x for x in node if isinstance(x, list) and x[0] == "pin")[1].strip('"')
                    s[f"{ref}.{pin}"] = name
b = pcbnew.LoadBoard(os.path.join(K, proj, f"{proj}.kicad_pcb"))
p = {f"{fp.GetReference()}.{pd.GetNumber()}": (pd.GetNetname() or "NC") for fp in b.GetFootprints() for pd in fp.Pads() if pd.GetNumber()}

print("== connections")
bad = [k for k, v in exp.items() if v != "NC" and not s.get(k, "").endswith(v)]
check(not bad, f"schematic matches the design file pin for pin ({sum(v != 'NC' for v in exp.values())} connections){'' if not bad else ': ' + str(bad[:5])}")
bad = [k for k, v in exp.items() if v == "NC" and k in s and not s[k].startswith("unconnected")]
check(not bad, "unused pins are left unconnected in the schematic")
bad = [k for k, v in exp.items() if p.get(k, "NC") != v]
check(not bad, f"board pads carry the design nets{'' if not bad else ': ' + str(bad[:5])}")
bad = [k for k, n in enumerate(D.LINK, 1) if p.get(f"J1.{k}") != D.PI_PINS[n][0]]
check(not bad, f"every Pi link header pin carries the signal of the Pi pin printed beside it (spec GPIO map, headlight on 24){'' if not bad else ': ' + str(bad)}")
check(sorted(D.LINK) == sorted(D.PI_PINS) and len(set(D.LINK)) == 20, "all 20 Pi pins the design uses are on the link header, none twice")
j1 = {pd.GetNumber(): pd.GetPosition() for fp in b.GetFootprints() if fp.GetReference() == "J1" for pd in fp.Pads()}
err = max(abs(j1[str(k)].x / 1e6 - D.link_pad_xy(k)[0]) + abs(j1[str(k)].y / 1e6 - D.link_pad_xy(k)[1]) for k in range(1, 21))
check(err < 0.01, "link header pads sit where the silk pin numbers were printed")
silk = {}
for d_ in b.GetDrawings():
    if d_.GetClass() == "PCB_TEXT" and d_.GetLayer() == pcbnew.F_SilkS: silk.setdefault(d_.GetText(), []).append(d_.GetPosition())
bad = [n for k, n in enumerate(D.LINK, 1) if not any(abs(t.x / 1e6 - D.link_pad_xy(k)[0]) < 2.6 and abs(t.y / 1e6 - D.link_pad_xy(k)[1]) < 0.1 for t in silk.get(str(n), []))]
check(not bad, f"each header pin has its Pi pin number printed on the same line{'' if not bad else ': ' + str(bad)}")
hl = {(round(fp.GetPosition().x / 1e6, 2), round(fp.GetPosition().y / 1e6, 2)) for fp in b.GetFootprints() if fp.GetReference().startswith("H")}
check(hl == {(h[0], h[1]) for h in D.HOLES}, "4 carrier screws on the deck's existing holes")

print("== board rules")
rpt = open(os.path.join(K, proj, "drc_report.txt")).read()
errs = rpt.count("Severity: error"); unc = int(re.search(r"Found (\d+) unconnected", rpt).group(1))
check(errs == 0, f"DRC: {errs} errors"); check(unc == 0, f"DRC: {unc} unconnected (fully routed)")
w = re.findall(r"^\[([a-z_]+)\][^\n]*\n[^\n]*Severity: warning", rpt, re.M)
print("      warnings: " + ", ".join(f"{x} x{w.count(x)}" for x in sorted(set(w))))
bb = b.GetBoardEdgesBoundingBox()
check(abs(bb.GetWidth() / 1e6 - 0.1 - D.BOARD_W) < 0.02 and abs(bb.GetHeight() / 1e6 - 0.1 - D.BOARD_L) < 0.02, "outline 41.8 x 83 (battery-bay deck)")
widths = {}
for t in b.GetTracks():
    if t.Type() == pcbnew.PCB_TRACE_T: widths.setdefault(t.GetNetname(), set()).add(round(t.GetWidth() / 1e6, 2))
pw = [n for n in ("PACK", "PACK_F", "SW", "GND") if min(widths.get(n, {0})) < 1.0] + [n for n in ("VM", "5V") if min(widths.get(n, {0})) < 0.8]
check(not pw, "battery traces 1.0 mm; 5 V and 6 V rails 0.8 mm")

print("== 3D fit (parts and plug-in boards)")
probs, _, _ = D.checks()
clash = [x for x in probs if x.startswith("CLASH") or x.startswith("OUT")]
check(not clash, f"no two parts occupy the same space{'' if not clash else ': ' + str(clash)}")
tall = sorted(((z1, lbl) for lbl, x0, y0, x1, y1, z0, z1 in D.boxes() if not lbl.startswith("hole")), reverse=True)[:4]
print("INFO  tallest above the carrier (for the new lid): " + ", ".join(f"{l} {z:.1f}" for z, l in tall))
print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
