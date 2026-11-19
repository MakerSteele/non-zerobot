#!/bin/bash
# Full pipeline: design.py -> KiCad projects -> autoroute -> verify -> fab outputs
set -e
cd "$(dirname "$0")"
python3 build.py
python3 route.py ../kicad/zerobot_carrier 200 | grep -E "Found|imported"
python3 verify.py
./export.sh > /dev/null
echo "done"
