"""Import a Freerouting .ses into a pcbnew BOARD (KiCad 7's ImportSpecctraSES needs the GUI)."""
import re, pcbnew
from kicad_text import parse
def find(node, key):
    for c in node:
        if isinstance(c, list) and c and c[0] == key: yield c
def import_ses(board, path):
    root = parse(open(path).read())[0]
    routes = next(find(root, "routes"))
    res = next(find(routes, "resolution")); div = float(res[2])            # e.g. um 10
    to_nm = lambda v: int(round(float(v) / div * 1000))                    # um -> nm
    layers = {"F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu}
    nout = next(find(routes, "network_out"))
    n_tr = n_via = 0
    for net in find(nout, "net"):
        ni = board.FindNet(net[1].strip('"'))
        for w in find(net, "wire"):
            p = next(find(w, "path")); layer = layers[p[1].strip('"')]; width = to_nm(p[2])
            pts = [(to_nm(p[i]), -to_nm(p[i + 1])) for i in range(3, len(p), 2)]
            for a, b in zip(pts, pts[1:]):
                t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(*a)); t.SetEnd(pcbnew.VECTOR2I(*b))
                t.SetWidth(width); t.SetLayer(layer); t.SetNet(ni); board.Add(t); n_tr += 1
        for v in find(net, "via"):
            m = re.search(r"(\d+):(\d+)_um", v[1])
            dia, drill = (int(m.group(1)) * 1000, int(m.group(2)) * 1000) if m else (800000, 400000)
            via = pcbnew.PCB_VIA(board); via.SetPosition(pcbnew.VECTOR2I(to_nm(v[2]), -to_nm(v[3])))
            via.SetWidth(dia); via.SetDrill(drill); via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); via.SetNet(ni)
            board.Add(via); n_via += 1
    return n_tr, n_via
