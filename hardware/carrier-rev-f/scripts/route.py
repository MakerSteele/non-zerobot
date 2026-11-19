"""Autoroute with Freerouting, add ground pours, run DRC, export Gerbers/drill/STEP.
Usage: python3 route.py <project_dir> [passes]"""
import os, sys, subprocess, json, glob, shutil
import pcbnew

d = sys.argv[1]; passes = sys.argv[2] if len(sys.argv) > 2 else "40"
name = os.path.basename(d.rstrip("/"))
pcb = os.path.join(d, f"{name}.kicad_pcb")
MM = 1e6
b = pcbnew.LoadBoard(pcb)

# remove any old tracks/zones so re-runs start clean
for t in list(b.GetTracks()): b.Remove(t)
for z in list(b.Zones()):
    if not z.GetIsRuleArea(): b.Remove(z)

dsn = os.path.join(d, "route.dsn"); ses = os.path.join(d, "route.ses")
assert pcbnew.ExportSpecctraDSN(b, dsn), "DSN export failed"
r = subprocess.run(["xvfb-run", "-a", "java", "-jar", "/tmp/fr.jar", "-de", dsn, "-do", ses, "-mp", passes],
                   capture_output=True, text=True, timeout=900)
log = (r.stdout + r.stderr)
open(os.path.join(d, "route.log"), "w").write(log)
assert os.path.exists(ses), "Freerouting produced no session file:\n" + log[-2000:]
from ses_import import import_ses
print("imported tracks/vias:", import_ses(b, ses))

# ground pours both sides
gnd = b.FindNet("GND")
bb = b.GetBoardEdgesBoundingBox()
for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
    z = pcbnew.ZONE(b); z.SetZoneName('GND pour'); z.SetLayer(layer); z.SetNet(gnd); z.SetLocalClearance(int(0.3 * MM))
    z.SetMinThickness(int(0.25 * MM)); z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetThermalReliefGap(int(0.4 * MM)); z.SetThermalReliefSpokeWidth(int(0.5 * MM))
    ol = z.Outline(); ol.NewOutline()
    for x, y in ((bb.GetLeft(), bb.GetTop()), (bb.GetRight(), bb.GetTop()), (bb.GetRight(), bb.GetBottom()), (bb.GetLeft(), bb.GetBottom())):
        ol.Append(x, y)
    b.Add(z)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.Save(pcb)
for f in (dsn, ses): os.remove(f)

# DRC
rpt = os.path.join(d, "drc_report.txt")
pcbnew.WriteDRCReport(b, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True)
txt = open(rpt).read()
print(txt[:200])
import re
for k in ("violations", "unconnected", "footprint errors"):
    m = re.search(r"\*\* Found (\d+) (DRC violations|unconnected pads|Footprint errors)", txt)
print(re.findall(r"\*\* Found (\d+) ([^*\n]+)", txt))
tracks = [t for t in b.GetTracks()]
print("tracks+vias:", len(tracks), " vias:", sum(1 for t in tracks if t.Type() == pcbnew.PCB_VIA_T))
