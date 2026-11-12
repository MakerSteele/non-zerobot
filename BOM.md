# Bill of materials

Everything needed for **one** Non-Zerobot. Quantities are per robot.

- **DigiKey parts** have exact manufacturer part numbers. To order them in one go, upload [`docs/bom/digikey_bom_per_kit.csv`](docs/bom/digikey_bom_per_kit.csv) with DigiKey's **"Upload a list"** tool (BOM Manager) and set the number of kits.
- **Amazon parts:** the links are **searches** for now. The exact listings used on the original robot will be added later. Any part matching the spec works.
- **Planning several kits?** [`docs/bom/kit_bom_calculator.xlsx`](docs/bom/kit_bom_calculator.xlsx) works out packs and cost for any number of kits.

> ⚠️ Most of these parts go on the **Rev G carrier board**. Its files will be added once a Rev G board has been built and tested. Prices are rough 2026 US prices, so check before you order.

---

## Your choice: Pi and camera

| Part | Notes | Approx. price |
|---|---|---|
| **Raspberry Pi**: 3B+ (reference build), 3B, 3A+, Zero 2 W or Zero W | Zeros must be the **W** version (with WiFi) | $10–35 |
| **Camera**: OV5647, Camera Module 3, or AI Camera | OV5647 is the cheapest | $12–35 |
| **Camera ribbon** | Pi 3 boards: 15-to-15 (usually comes with the camera). Pi Zero boards: **15-to-22** | $3 |
| **microSD card**, 16 GB, A1 rated | | $6 |

## Motors, driver and sensing

| Qty | Part | Exact part / where | Notes |
|---|---|---|---|
| 1 pair | Hiwonder TT gearmotor with hall encoder | [Hiwonder](https://www.hiwonder.com/products/tt-motor-plastic) (encoder version) | Cables included. Use them exactly as they come |
| 1 | TB6612FNG motor driver board, "HW-166" | [Amazon search](https://www.amazon.com/s?k=TB6612FNG+HW-166+module) | Generic red board |
| 1 | ADS1115 ADC board, CJMCU | [Amazon search](https://www.amazon.com/s?k=ADS1115+CJMCU+module) | Battery voltage reading |

## Power

| Qty | Part | Exact part / where | Notes |
|---|---|---|---|
| 2 | 18650 cell, **protected**, ~3400 mAh, button top | [Amazon search](https://www.amazon.com/s?k=protected+18650+3400mAh+button+top) | Matched pair from a reputable seller. Wired in parallel |
| 1 | Charger + 5 V boost combo board, USB-C (G4056A) | [Amazon search](https://www.amazon.com/s?k=type-c+5V+2A+18650+charge+boost+module) | Charges the pack and powers the Pi |
| 1 | Walfront 6 V boost converter, **"without pin"** option | [Amazon search](https://www.amazon.com/s?k=Walfront+2Pcs+DC-DC+Boost+Converter+Module+2.6-5.5+to+5V%2F6V%2F9V%2F12V) | Motor power. Choose the **6 V** version |
| 1 | DPDT rocker switch, ON-OFF, rated over 4 A DC | [Amazon search](https://www.amazon.com/s?k=DPDT+rocker+switch+6+pin+on+off) | 🟫 Exact part still to be chosen (must fit the shell cutout) |
| 1 | Resettable fuse, 4 A hold | DigiKey [MF-RG400-0](https://www.digikey.com/en/products/result?keywords=MF-RG400-0) (Bourns) | Right after the battery plug |

## Carrier board parts (all from DigiKey)

| Qty | Part | Manufacturer part number | Used for |
|---|---|---|---|
| 1 | Rev G carrier PCB | JLCPCB (files coming) | 2-layer, 1.6 mm. JLCPCB's minimum order is 5 boards |
| 2 | Female header 1×8, 2.54 mm, 8.5 mm tall | [PPTC081LFBN-RC](https://www.digikey.com/en/products/result?keywords=PPTC081LFBN-RC) | Motor driver sockets |
| 1 | Female header 1×10, 2.54 mm, 8.5 mm tall | [PPTC101LFBN-RC](https://www.digikey.com/en/products/result?keywords=PPTC101LFBN-RC) | Battery sensor socket |
| 1 | Female header 1×3, 2.54 mm, 8.5 mm tall | [PPTC031LFBN-RC](https://www.digikey.com/en/products/result?keywords=PPTC031LFBN-RC) | Motor boost socket |
| 1 | Male header 2×10, 2.54 mm | [PRPC010DAAN-RC](https://www.digikey.com/en/products/result?keywords=PRPC010DAAN-RC) | Pi link header |
| 1 | Male header 1×40 breakaway (uses 7 pins) | [PRPC040SAAN-RC](https://www.digikey.com/en/products/result?keywords=PRPC040SAAN-RC) | Power jumpers + boost pins. One strip covers about 5 robots |
| 2 | Jumper cap, 2.54 mm | [QPC02SXGN-RC](https://www.digikey.com/en/products/result?keywords=QPC02SXGN-RC) | Pi power and motor power jumpers |
| 1 | JST-XH 2-pin vertical socket | [B2B-XH-A](https://www.digikey.com/en/products/result?keywords=B2B-XH-A) | Battery plug |
| 2 | JST-XH 5-pin vertical socket | [B5B-XH-A](https://www.digikey.com/en/products/result?keywords=B5B-XH-A) | Headlight plugs |
| 2 | JST-PH 6-pin vertical socket | [B6B-PH-K-S](https://www.digikey.com/en/products/result?keywords=B6B-PH-K-S) | Motor sockets |
| 3 | PN2222A NPN transistor, TO-92 | [PN2222ATA](https://www.digikey.com/en/products/result?keywords=PN2222ATA) | Red, green, blue headlight switches. **Not P2N2222A** (different pin order) |
| 3 | 1 kΩ resistor, ¼ W 1% | [MFR-25FBF52-1K](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-1K) | Transistor base resistors |
| 1 | 270 Ω resistor, ¼ W 1% | [MFR-25FBF52-270R](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-270R) | Red LED. 🟫 Value still to be confirmed |
| 2 | 180 Ω resistor, ¼ W 1% | [MFR-25FBF52-180R](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-180R) | Green and blue LEDs. 🟫 Value still to be confirmed |
| 2 | 10 kΩ resistor, ¼ W 1% | [MFR-25FBF52-10K](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-10K) | Battery voltage divider |
| 1 | 470 µF 6.3 V electrolytic, 6.3 mm, 2.5 mm leads | [UVR0J471MED](https://www.digikey.com/en/products/result?keywords=UVR0J471MED) | Battery capacitor |
| 1 | 220 µF 16 V electrolytic, 6.3 mm, 2.5 mm leads | [UVZ1C221MED](https://www.digikey.com/en/products/result?keywords=UVZ1C221MED) | Motor power capacitor |
| 1 | 100 nF ceramic, 50 V, 2.5 mm leads | [FG18X7R1H104KNT06](https://www.digikey.com/en/products/result?keywords=FG18X7R1H104KNT06) | Battery sense filter |

## Headlights and wiring

| Qty | Part | Where | Notes |
|---|---|---|---|
| 2 | 8 mm RGB LED, **common anode** | [Amazon search](https://www.amazon.com/s?k=8mm+RGB+LED+common+anode) | Common anode goes to + |
| 2 | JST-XH 5-pin pigtail, pre-crimped, single end | [Amazon search](https://www.amazon.com/s?k=JST+XH+2.5mm+5+pin+pre-crimped+cable+single+end) | Solder the bare end to the LEDs |
| 1 | JST-XH 2-pin pigtail, pre-crimped, single end | [Amazon search](https://www.amazon.com/s?k=JST+XH+2.5mm+2+pin+pre-crimped+cable+single+end) | Battery lead. **Check the red wire lands on +** |
| 20 | Dupont jumper wire, female–female, 20 cm | [Amazon search](https://www.amazon.com/s?k=female+to+female+dupont+jumper+wire+20cm+40+pin) | Carrier to Pi link |
| ~1 m | Silicone wire 20 AWG, red + black | [Amazon search](https://www.amazon.com/s?k=20+AWG+silicone+wire+red+black) | Switch and charger wiring |
| — | Heatshrink | Any | |

## Mounting and printed parts

| Qty | Part | Notes |
|---|---|---|
| 4 | M2 × 8 nylon screw + 3–4 mm spacer | [Amazon search](https://www.amazon.com/s?k=M2+nylon+standoff+screw+assortment): mounts the carrier |
| 1 set | Printed chassis, lid, wheels, rear skid, battery holder | Based on ZDV's ZeroBot Pro V1 ([Thingiverse 3232512](https://www.thingiverse.com/thing:3232512)). 🟫 Modified STL files to be added |

## Tools

Soldering iron and solder, flush cutters, multimeter, small screwdriver, a USB-C phone charger, and a micro-USB charger for setting up the Pi.

---

**Rough cost:** about $75–90 per robot for everything above (at a 5-kit order), plus your Pi and camera. A reference build with a Pi 3B+, SD card and OV5647 comes to about $140. These are estimates, so check current prices.
