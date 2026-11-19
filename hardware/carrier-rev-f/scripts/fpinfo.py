import pcbnew
def crt_bbox(fp):
    xs=[];ys=[]
    for g in fp.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            b=g.GetBoundingBox(); xs+= [b.GetLeft(),b.GetRight()]; ys+=[b.GetTop(),b.GetBottom()]
    if not xs:
        b=fp.GetBoundingBox(False,False); return b.GetLeft(),b.GetTop(),b.GetRight(),b.GetBottom()
    return min(xs),min(ys),max(xs),max(ys)
if __name__=="__main__":
    for lib,n in [('Connector_IDC','IDC-Header_2x10_P2.54mm_Vertical'),('TerminalBlock_Phoenix','TerminalBlock_Phoenix_PT-1,5-2-3.5-H_1x02_P3.50mm_Horizontal'),('Package_TO_SOT_THT','TO-92_Inline'),('Fuse','Fuse_Bourns_MF-RG400'),('Connector_PinSocket_2.54mm','PinSocket_2x20_P2.54mm_Vertical'),('Connector_JST','JST_PH_B6B-PH-K_1x06_P2.00mm_Vertical'),('Connector_JST','JST_XH_B5B-XH-A_1x05_P2.50mm_Vertical'),('Resistor_THT','R_Axial_DIN0207_L6.3mm_D2.5mm_P2.54mm_Vertical'),('Capacitor_THT','CP_Radial_D6.3mm_P2.50mm'),('Capacitor_THT','C_Disc_D5.0mm_W2.5mm_P5.00mm'),('Connector_PinSocket_2.54mm','PinSocket_1x07_P2.54mm_Vertical'),('Connector_PinHeader_2.54mm','PinHeader_1x02_P2.54mm_Vertical')]:
        fp=pcbnew.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty',n)
        l,t,r,b=crt_bbox(fp)
        print(n, round((r-l)/1e6,2), round((b-t)/1e6,2), [(p.GetNumber(), round(p.GetPosition().x/1e6,2), round(p.GetPosition().y/1e6,2)) for p in list(fp.Pads())[:3]])
