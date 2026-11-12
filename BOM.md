# Bill of materials

Everything needed for **one** Non-Zerobot. Quantities are per robot.

- **DigiKey parts** have exact manufacturer part numbers. To order them in one go, upload [`docs/bom/digikey_bom_per_kit.csv`](docs/bom/digikey_bom_per_kit.csv) with DigiKey's **"Upload a list"** tool (BOM Manager) and set the number of kits.
- **Amazon links** go to the exact listings used on the original robot. Parts marked "any" are generic, so anything matching the spec works.
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
| 1 | TB6612FNG motor driver board, "HW-166" | [Amazon](https://a.co/d/0c9i08Xj): WWZMDiB 4-pack | Red board. Comes with header strips |
| 1 | ADS1115 ADC board (blue, 10-pin, VDD … A3) | [Amazon](https://a.co/d/07sgLrBt): Qoroos 4-pack | Battery voltage reading. Comes with a header strip |

## Power

| Qty | Part | Exact part / where | Notes |
|---|---|---|---|
| 2 | 18650 cell, **protected**, ~3400 mAh, button top | Any reputable seller ([example search](https://www.amazon.com/s?k=protected+18650+3400mAh+button+top)) | The original robot's came from a local battery store. Buy a matched pair. Wired in parallel |
| 1 | 18650 charger + boost board, USB-C (TP4056 type) | [Amazon](https://a.co/d/0flYN2TI): Teyleten Robot 10-pack | Charges the pack and powers the Pi. **Has an adjustment screw: set its output to about 5.1 V before it touches the Pi** (see [ASSEMBLY.md](ASSEMBLY.md) 5.3) |
| 1 | Walfront 6 V boost converter (2-pack, ~$9) | [Amazon](https://a.co/d/09j4Al6v) | Motor power. Pick the **6 V, with pin** option (the only one listed). Its header has to be moved so the pins point down: see [ASSEMBLY.md](ASSEMBLY.md) 4.2 |
| 1 | Mini rocker switch KCD1, **4-pin ON-OFF (two poles)**, VEXUNGA 10-pack | [Amazon](https://a.co/d/02PLmvfy) | Pick the "4 Pin ON/OFF" style. The listing title says SPST, but the 4-pin version switches two separate circuits, which is what the robot needs. Rated 10 A 250 V AC (no DC rating listed) |
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
| 1 | 270 Ω resistor, ¼ W 1% | [MFR-25FBF52-270R](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-270R) | Red LED (about 5 mA per LED) |
| 2 | 180 Ω resistor, ¼ W 1% | [MFR-25FBF52-180R](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-180R) | Green and blue LEDs (about 5 mA per LED) |
| 2 | 10 kΩ resistor, ¼ W 1% | [MFR-25FBF52-10K](https://www.digikey.com/en/products/result?keywords=MFR-25FBF52-10K) | Battery voltage divider |
| 1 | 470 µF 6.3 V electrolytic, 6.3 mm, 2.5 mm leads | [UVR0J471MED](https://www.digikey.com/en/products/result?keywords=UVR0J471MED) | Battery capacitor |
| 1 | 220 µF 16 V electrolytic, 6.3 mm, 2.5 mm leads | [UVZ1C221MED](https://www.digikey.com/en/products/result?keywords=UVZ1C221MED) | Motor power capacitor |
| 1 | 100 nF ceramic, 50 V, 2.5 mm leads | [FG18X7R1H104KNT06](https://www.digikey.com/en/products/result?keywords=FG18X7R1H104KNT06) | Battery sense filter |

## Headlights and wiring

| Qty | Part | Where | Notes |
|---|---|---|---|
| 2 | 10 mm RGB LED, **common anode**, clear | [Amazon](https://a.co/d/0aHCYMcc): Chanzon 50-pack | Long leg (common anode) goes to +. The bag lists red 2.0–2.2 V, green/blue 3.0–3.2 V at 20 mA |
| 2 | JST-XH 5-pin pigtail, pre-crimped, 150 mm | [Amazon](https://a.co/d/09ycJAOr): ALAMSCN 20-pair kit | Solder the bare end to the LEDs. The kit also has matching board headers, so if you buy it you can skip the B5B-XH-A sockets on the DigiKey list |
| ½ | JST-XH 2-pin extension cable, 500 mm (Adafruit 5497) | DigiKey [1528-5497-ND](https://www.digikey.com/en/products/detail/adafruit-industries-llc/5497/16680577) | Battery lead, new for Rev G. Cut it in half and keep the **socket** end: one cable makes leads for 2 robots. **Check the red wire lands on +** |
| 20 | Dupont jumper wire, **female–female**, 2.54 mm, 20 cm | [Amazon](https://a.co/d/0dx3AREg): HiLetgo 200-piece assortment | Carrier to Pi link. The assortment has several kinds; use the female–female 2.54 mm strip |
| ~1 m | Hookup wire, 20–22 AWG, red + black | Any. Silicone wire is easiest to work with | Switch and charger wiring |
| — | Heatshrink | Any | |

## Mounting and printed parts

| Qty | Part | Notes |
|---|---|---|
| 4 | M2 self-tapping pan-head screw, 4–6 mm long (M2 × 4 on the original) | [Amazon](https://a.co/d/059v6QbO): HanTof 750-piece M1.7–M3 assortment | Mounts the carrier. They screw straight into the standoffs printed on the chassis deck, so no spacers needed. **Use M2, not M2.3:** the board's holes are 2.2 mm |
| 1 set | Printed chassis, lid, wheels, rear skid, battery holder | Based on ZDV's ZeroBot Pro V1 ([Thingiverse 3232512](https://www.thingiverse.com/thing:3232512)). 🟫 Modified STL files to be added |

## Planned parts (not in the design yet)

These have been bought for future versions but **aren't in the build yet**. Don't fit them.

| Qty | Part | Where | Notes |
|---|---|---|---|
| 1 | 1S 3.7 V 4 A battery protection board (BMS), DIANN 10-pack | [Amazon](https://a.co/d/0fw7gXr4) | 🟫 Not in the carrier or the wiring yet |

## Tools

Soldering iron and solder, flush cutters, multimeter, small screwdriver, a USB-C phone charger, and a micro-USB charger for setting up the Pi.

---

**Rough cost:** about $75–90 per robot for everything above (at a 5-kit order), plus your Pi and camera. A reference build with a Pi 3B+, SD card and OV5647 comes to about $140. These are estimates, so check current prices.
