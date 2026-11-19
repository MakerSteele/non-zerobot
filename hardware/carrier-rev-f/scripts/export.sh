#!/bin/bash
# Gerbers + drill (JLCPCB-ready zip), schematic PDF, board renders, STEP for SolidWorks
set -e
K=/home/claude/carrier/kicad
for P in zerobot_carrier; do
  D=$K/$P; O=$D/fab; rm -rf $O; mkdir -p $O/gerbers
  kicad-cli pcb export gerbers -o $O/gerbers/ --layers F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts --subtract-soldermask --use-drill-file-origin $D/$P.kicad_pcb >/dev/null
  kicad-cli pcb export drill -o $O/gerbers/ --format excellon --excellon-separate-th --generate-map --map-format gerberx2 $D/$P.kicad_pcb >/dev/null
  (cd $O/gerbers && zip -q ../${P}_gerbers_JLCPCB.zip *)
  kicad-cli sch export pdf -o $O/${P}_schematic.pdf $D/$P.kicad_sch >/dev/null
  kicad-cli pcb export step --subst-models -f -o $O/${P}.step $D/$P.kicad_pcb >/dev/null 2>&1 || echo "step export failed for $P"
  kicad-cli pcb export svg -o $O/top.svg --layers F.Cu,F.SilkS,F.Mask,Edge.Cuts --page-size-mode 2 --exclude-drawing-sheet $D/$P.kicad_pcb >/dev/null
  kicad-cli pcb export svg -o $O/bottom.svg --layers B.Cu,B.SilkS,Edge.Cuts --page-size-mode 2 --exclude-drawing-sheet --mirror $D/$P.kicad_pcb >/dev/null
  ls $O
done
