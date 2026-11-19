"""Reads and writes the bracketed text format KiCad uses for its files."""
import re

TOK = re.compile(r'\s*(\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+)', re.S)

def parse(text):
    stack, cur = [], []
    for m in TOK.finditer(text):
        t = m.group(1)
        if t == "(":
            stack.append(cur); cur = []
        elif t == ")":
            done = cur; cur = stack.pop(); cur.append(done)
        else:
            cur.append(t)
    return cur

def dump(x, ind=0):
    if not isinstance(x, list):
        return x
    if all(not isinstance(e, list) for e in x):
        return "(" + " ".join(x) + ")"
    pad = "  " * (ind + 1)
    parts = [x[0]] if x and not isinstance(x[0], list) else []
    rest = x[1:] if parts else x
    s = "(" + " ".join(parts)
    for e in rest:
        if isinstance(e, list):
            s += "\n" + pad + dump(e, ind + 1)
        else:
            s += " " + e
    return s + ")"

def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

_libcache = {}
def lib_symbol(lib_id):
    """Return the symbol s-expr (list) renamed to lib:name, plus its pins [(num, x, y, angle)]."""
    lib, name = lib_id.split(":")
    if lib not in _libcache:
        _libcache[lib] = parse(open(f"/usr/share/kicad/symbols/{lib}.kicad_sym").read())[0]
    for e in _libcache[lib]:
        if isinstance(e, list) and e[0] == "symbol" and e[1] == q(name):
            sym = [x for x in e]
            sym[1] = q(lib_id)
            pins = []
            def walk(n):
                for c in n:
                    if isinstance(c, list):
                        if c[0] == "pin":
                            at = next(k for k in c if isinstance(k, list) and k[0] == "at")
                            num = next(k for k in c if isinstance(k, list) and k[0] == "number")[1].strip('"')
                            pins.append((num, float(at[1]), float(at[2]), int(float(at[3]))))
                        else:
                            walk(c)
            walk(sym)
            return sym, pins
    raise KeyError(lib_id)
