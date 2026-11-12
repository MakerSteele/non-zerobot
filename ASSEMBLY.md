# Assembling the Non-Zerobot

Step-by-step hardware assembly: soldering the carrier board, wiring power, plugging in the modules, and the first drive.

> ⚠️ **Status: written for the Rev G carrier board, which is still a draft.** The board files will be added to this repo once a Rev G board has been built and tested. Steps marked 🟫 **To be added** are still open.

**You'll need:** soldering iron and solder, flush cutters, a multimeter, a small screwdriver, a phone with a web browser, a USB-C phone charger, and a micro-USB charger for setting up the Pi. Parts are in [BOM.md](BOM.md).

## Three rules that protect your robot

1. **Measure before you fit a jumper.** The Pi power and motor power jumpers go on only after the guide says the voltages are right.
2. **Never rearrange the wires inside a plug.** Use the battery, motor and headlight leads as they come, and check the battery lead's red wire is on +.
3. **Power off before touching the camera ribbon or plugging in a module.**

🛑 **Stop and check** boxes are the steps where a mistake can damage parts. Don't skip them.

---

## Contents

1. [Get ready](#1-get-ready)
2. [Set up the software](#2-set-up-the-software)
3. [Solder the carrier](#3-solder-the-carrier)
4. [Prepare the modules](#4-prepare-the-modules)
5. [Power](#5-power)
6. [Plug in the modules](#6-plug-in-the-modules)
7. [The Pi](#7-the-pi)
8. [Motors and headlights](#8-motors-and-headlights)
9. [Camera](#9-camera)
10. [First drive](#10-first-drive)
11. [Fix it](#11-fix-it)

---

## 1. Get ready

### 1.1 Lay out your parts
Spread everything on a clear, non-metal surface and tick each part off against [BOM.md](BOM.md). Keep the small parts in their bags until the step that uses them.

✅ **Done when:** every part on the list is in front of you.

### 1.2 Check the batteries
Set your multimeter to DC volts. Measure each 18650 cell: red probe on the raised end (+), black on the flat end (−). Write both readings down.

🛑 **Stop and check:** if the two cells differ by more than 0.1 V, don't put them in the pack together. Cells side by side always settle to the same voltage, so a big difference pushes a surge of current from one into the other.

✅ **Done when:** both cells read between 3.6 V and 4.2 V and are within 0.1 V of each other.

> Use **protected** cells only. A protected cell has a tiny circuit that cuts it off on a short. If a cell suddenly reads near 0 V, the protection has tripped. Find the short before charging it again.

### 1.3 Get to know the carrier board
Hold the carrier with the white printing up. The end with the two **headlight** sockets is the **front**. The **battery** plug and the **switch** pads are at the **rear**.

These marks are used all through the build:
- **+** next to a pad: the positive side
- **G**: the ground end of a socket
- **Square pads**: pin 1, or + on parts that have a + and −
- **PARTS UP**, **IN** / **OUT**: which way a module or wire goes

✅ **Done when:** you can point to the front, the rear, the battery plug, both motor sockets and the Pi link header (the 2-row block of 20 pins).

---

## 2. Set up the software

Do this first, on the bench, with the Pi on its own USB power. Follow **[INSTALL.md](INSTALL.md)** steps 1–4, then come back here.

🛑 **Stop and check:** choose **Raspberry Pi OS Lite (32-bit)**. A 64-bit system won't start on a Pi Zero W, and the desktop version is too heavy for the robot.

✅ **Done when:** `http://ROBOTNAME.local:5000` opens the control page. There's no camera picture yet, and that's fine.

---

## 3. Solder the carrier

Solder the lowest parts first so the board lies flat while you work. Parts go in from the printed side and are soldered on the back. Snip the legs short afterwards.

### 3.1 Resistors and the small capacitor
- The two **battery divider** resistors (10 kΩ) near the rear
- The **headlight** resistors in the middle: **270 Ω** for red, **180 Ω** for green and blue, and **1 kΩ** for the three transistor bases. Fill the red, green and blue positions only. **Leave the white positions empty.**
- The small **battery filter** capacitor (100 nF) beside the divider

✅ **Done when:** every resistor sits flat, and the snipped legs stick out no more than 2 mm.

### 3.2 Headlight transistors
Each transistor has a flat face with writing on it. The flat face goes toward the **rear** of the board, matching the outline. Fit three on **R**, **G** and **B**. **Leave W empty.** Leave about 2 mm of leg above the board.

🛑 **Stop and check:** the transistor must say **PN2222A**. A **P2N2222A** looks the same, but its legs are in a different order and it won't work here.

✅ **Done when:** three transistors sit on R, G and B with their flat faces to the rear.

### 3.3 Sockets and headers
For each one, push it in, solder **one** corner pin, check it sits flat and straight, then solder the rest.
- Two 8-pin sockets for the **motor driver**
- One 10-pin socket for the **battery sensor**
- One 3-pin socket for the **motor boost**, at the rear right
- The 2×10 pin header for the **Pi link** (pins point up)
- The two 2-pin headers for the **Pi power** and **motor power** jumpers. **Don't fit the jumper caps yet.**
- The **fuse**

✅ **Done when:** everything stands straight with no solder bridges between neighbouring pins.

### 3.4 Connectors
These are keyed. Match the plastic housing to the printed outline before soldering.
- Two 6-pin **motor** sockets
- Two 5-pin **headlight** sockets at the front
- The 2-pin **battery** plug at the rear left

✅ **Done when:** each housing sits flat and matches its outline, with the G and + marks still visible beside them.

### 3.5 The two big capacitors
The bigger can is for the **battery** (470 µF) and the smaller one for the **motor power** (220 µF). The **long leg (+) goes in the square pad**, next to the + mark. The stripe marks the − side.

🛑 **Stop and check:** on both capacitors, the long leg is in the square + pad and the stripe faces away from the + mark. A capacitor in backwards can burst when powered.

---

## 4. Prepare the modules

### 4.1 Headers on the motor driver and battery sensor
The motor driver (TB6612) and battery sensor (ADS1115) come with loose header strips. Push the strips into the carrier's sockets, sit the module on top, and solder. The sockets hold the pins straight. Then pull the modules back out for now.

### 4.2 The motor boost's pins
The motor boost is the small board with a coil on it. Its three pins (`Vi`, `G`, `Vo`) must point **down, away from the parts side**, so it lies flat with its parts facing up.

The boost comes with its pin header already soldered on, so move it:
1. Desolder the 3-pin header. Solder wick or a solder sucker works best. Heat one pin at a time and rock the header out.
2. Clear the three holes.
3. Fit a straight 3-pin header from the parts-free side, so the pins point **down**, and solder it on the parts side.

🟫 **To be added:** photos of this step.

> The boost is fixed at 6 V, with no adjusting screw. Upside down, its in and out swap places and it simply gives no output.

---

## 5. Power

### 5.1 Check the battery lead
Hold the battery lead's plug next to the carrier's battery plug, turned the only way it fits. The lead's **red** wire must line up with the **+** mark. Put the cells in the pack and measure the plug's contacts, red probe on the red wire's contact.

🛑 **Stop and check:** the red wire is on the + side and the meter reads a **positive** voltage. These plugs have no colour standard, so check every lead. The fuse can't protect against reversed polarity.

### 5.2 Wire the power switch
The switch has two separate halves (poles). Four pads on the rear edge go to it:

| Carrier pads | Go to |
|---|---|
| `BATT SW` IN and OUT | One half of the switch |
| `PI 5V SW` IN and OUT | The other half |

🛑 **Stop and check:** keep each pair on its own half of the switch. If a BATT wire and a PI 5V wire end up on the same half, the battery is tied straight to the Pi's 5 V supply.

✅ **Done when:** with the meter on continuity and the switch ON, BATT IN beeps to BATT OUT, PI 5V IN beeps to PI 5V OUT, and no BATT pad beeps to any PI 5V pad. With the switch OFF, nothing beeps.

**Finding the two halves:** on the 4-pin KCD1 switch, each pair of tabs that beeps together on continuity with the switch ON is one half. With the switch OFF, nothing should beep. Work this out with the meter before soldering.

🟫 **To be added:** wire colours and lengths, and a photo of which tabs pair up.

### 5.3 Wire the charger board
The charger board sits in the shell with its USB-C port through the wall. It charges the battery and makes the Pi's 5 V.

| Charger board | Carrier |
|---|---|
| Battery side + and − | `CHG` pads (+ is the square pad) |
| 5 V output + and − | `5V IN` pads (+ is the square pad) |

🛑 **Stop and check:** each + goes to a square + pad, and the battery side and the 5 V side are not swapped.

**Set its output voltage first.** This board has a small adjustment screw (trimmer) and isn't always set to 5 V from the factory. Before wiring it to the carrier, connect a charged cell to its B+ and B− pads, put the meter on its output + and −, and turn the screw gently until it reads **5.1 V**.

🛑 **Stop and check:** the output reads 5.0–5.2 V. Too high can damage the Pi.

> The charger connects before the switch, so the robot charges while it's switched off. Its booster slowly drains the cells even when the robot is off, so unplug the battery if you're storing the robot for weeks.

### 5.4 Fit the motor boost and measure both supplies
Leave both jumper caps off. The motor driver, battery sensor and Pi stay disconnected.
1. Plug the motor boost into its 3-pin socket **parts up**. Seen from above with the pins at the rear, the order is **Vo left, G centre, Vi right**.
2. Plug in the battery and switch on.
3. On each jumper header, measure both pins to ground. One pin reads the supply and the other reads 0 V.
4. Switch off and check both now read 0 V.

🛑 **Stop and check:** only continue if the **motor** jumper reads **5.8–6.2 V** and the **Pi** jumper reads **4.9–5.25 V** with the switch on, and both drop to 0 V with it off. The Pi's 5 V pins skip its own input protection, so a wrong voltage goes straight in.

---

## 6. Plug in the modules

### 6.1 Motor driver
Battery unplugged. Match the pin names printed beside the sockets to the labels on the driver board. The `PWMA` row goes toward the **front** and the `VM` row toward the **rear**.

🛑 **Stop and check:** the driver's labels match the printed names. Turned round, it shorts the Pi's 3.3 V supply and the Pi won't start.

### 6.2 Battery sensor
The sensor lies flat over the resistors, with its `VDD` end toward the **front** to match the `VDD` and `A3` marks.

🛑 **Stop and check:** VDD is next to the VDD mark.

---

## 7. The Pi

### 7.1 Find pin 1
Turn the Pi over. One pad at one end of the 40-pin header is **square**: that's pin 1.
- **Pi 3A+, 3B, 3B+:** the end **away from the USB ports**
- **Pi Zero W, Zero 2 W:** the end nearest the **microSD slot**

Pins count in pairs across the row: 1 and 2, then 3 and 4, and so on. Need help finding a pin later? Run `python3 ~/zerobot/findpin.py`.

### 7.2 Mount the Pi in the lid
🟫 **To be added:** lid mounting steps come with the final lid design. A mount for the Pi Zero boards is still to be designed.

### 7.3 Wire the Pi link
Each of the 20 pins on the carrier's Pi link header has a Pi pin number printed beside it. Run one jumper wire from each header pin to that pin on the Pi. Do the power and ground wires first (Pi pins 1, 2, 4, 6, 14, 39), then the rest one at a time.

🛑 **Stop and check:** Pi pin 1 (3.3 V) and pin 2 (5 V) sit side by side. Swapped, they send 5 V into parts that only take 3.3 V. Check those two wires twice.

✅ **Done when:** with the meter on continuity, every carrier header pin beeps to the Pi pin printed beside it.

<details><summary>Pin map (for reference)</summary>

| Function | GPIO | Pi pin |
|---|---|---|
| 3.3 V | — | 1 |
| 5 V (feeds the Pi) | — | 2, 4 |
| Ground | — | 6, 14, 39 |
| I2C SDA / SCL (battery sensor) | 2 / 3 | 3 / 5 |
| Left motor forward / backward | 17 / 27 | 11 / 13 |
| Right motor forward / backward | 22 / 23 | 15 / 16 |
| Left encoder H1 / H2 | 5 / 6 | 29 / 31 |
| Right encoder H1 / H2 | 13 / 19 | 33 / 35 |
| Headlight red / green / blue | 12 / 16 / 26 | 32 / 36 / 37 |
| Headlight white (not fitted) | 24 | 18 |

</details>

### 7.4 Power the Pi
Switch off and take the SD card out for this test. Leave the motor jumper off. Fit the cap on the **Pi power** jumper, then switch on.

✅ **Done when:** the red power light comes on (a Pi Zero shows green instead). Switch off and put the SD card back.

---

## 8. Motors and headlights

### 8.1 Mount the motors, wheels and skid
🟫 **To be added:** chassis assembly steps come with the final chassis. The printed parts are based on ZDV's ZeroBot Pro V1 (see [CREDITS.md](CREDITS.md)).

### 8.2 Plug in the motors
Plug each motor's cable into its motor socket: left motor on the left, right on the right. The plug is keyed, and its **G** wire lands next to the **G** mark. Use the cables **exactly as they came**.

🛑 **Stop and check:** never rearrange the wires inside a motor plug. A plug wired differently puts motor power onto the speed sensor and can destroy it.

### 8.3 Plug in the headlights
Plug one headlight lead into each socket at the front, with the **+** wire next to the **+** mark. Each RGB LED's shared leg goes to **+**, and its red, green and blue legs go to **R**, **G** and **B**. **W** stays empty.

🟫 **To be added:** how the headlight leads are made, and the LED leg order.

---

## 9. Camera

🛑 **Stop and check:** power must be off whenever you connect or remove the camera ribbon.

- **Pi 3A+, 3B, 3B+:** use the long slot between the HDMI port and the audio jack. Lift the latch, slide the ribbon in with the **blue** backing toward the USB ports, and press the latch down.
- **Pi Zero W, Zero 2 W:** use the small slot on the short edge. These need a ribbon with one **narrow** end (15-to-22). Pull the latch out, slide the narrow end in with the contacts facing the board, and push the latch back.

At the camera end, use the wide end with the contacts toward the camera board.

| Camera | What it's like |
|---|---|
| OV5647 (Pi Camera v1) | Cheapest. Clear 640×480 video with a short delay. Focus by hand once |
| Camera Module 3 | Sharper and better in low light. The software sets the focus |
| AI Camera | Higher-resolution sensor. Used here as a regular camera |

The software detects the camera by itself, so there's nothing to configure.

🟫 **To be added:** camera mounting steps.

---

## 10. First drive

### 10.1 Switch on
Switch off, fit the cap on the **motor power** jumper, and check the SD card is in. Prop the robot up so the wheels are off the table. Switch on and wait about 90 seconds.

### 10.2 Connect from your phone
Join your phone to the same WiFi you set in Imager and open `http://ROBOTNAME.local:5000`.

✅ **Done when:** the control page shows live video, **PWR** shows 3.6–4.2 V, and **VER** is green.

### 10.3 Test the wheels
Wheels still off the table. Push the **left stick** gently forward: both wheels should turn forward. The **right stick** steers.

🛑 **Stop and check:** if a wheel hums but doesn't turn, or the battery reading drops sharply, switch off at once. That's a wiring fault that can damage the speed sensors.

> Motor direction is already set in the software for this board, so a wheel turning backwards means a build fault, not a setting to change. If your phone loses WiFi mid-drive, the robot stops itself within about a third of a second.

### 10.4 Check the speed sensors
Wheels off the table. Open `http://ROBOTNAME.local:5000/paircheck`. It runs each motor in turn and checks the right sensor counts.

✅ **Done when:** it says **CORRECT** and the **BAL** button is green.

### 10.5 Focus and tune
- **Focus:** on cameras without autofocus, aim at something with fine detail about 2 m away and turn the lens a few degrees at a time until it's sharp.
- **Turning too sharply:** lower the **STR** slider. It's remembered after restarts.
- **Drifting with BAL green:** usually a tyre or the skid. Move the **TRM** slider (negative slows the left wheel).
- **Minimum motor power:** open `http://ROBOTNAME.local:5000/stallcheck` with the wheels off the ground. It measures how much power each motor needs before it starts moving.

---

## 11. Fix it

| Symptom | Likely cause and fix |
|---|---|
| Pi power light stays off | Pi jumper missing, or the 5 V supply is wrong. Re-measure (4.9–5.25 V) and check the switch and charger wiring |
| Pi only starts with the carrier disconnected | The motor driver is turned round, which shorts 3.3 V. Check its labels match the printed names |
| Pi never starts after flashing | A 64-bit system on a Pi Zero W, or the wrong device picked in Imager. Re-flash with **Lite (32-bit)** |
| `ROBOTNAME.local` not found | Wait 2 minutes on the first start. Check the WiFi and hostname, or use the IP address from your router |
| Motors don't respond | Motor jumper missing, or no 6 V on the driver's `VM` pin. Check the Pi link wires on pins 11, 13, 15, 16 |
| Motor boost gives 0 V | It's upside down or in the wrong holes. It must lie parts up, with Vo, G and Vi on their marks |
| Wheel hums but won't turn | **Switch off at once.** The motor plug is wrong for this board |
| BAL turns red | Tap it to see why. A silent sensor usually means a loose motor plug |
| Robot reboots while driving | Low battery. `vcgencmd get_throttled` should say `0x0` |
| PWR says unavailable | Battery sensor not answering. Check VDD is at the mark and the wires on pins 1, 3, 5. `i2cdetect -y 1` should show `48` |
| Camera detected but no picture | Damaged ribbon. Power off and swap in a spare |
| Headlights wrong or missing a colour | Check + is on +, the legs are on R/G/B (not W), and the transistors are PN2222A facing the rear |
| Battery flat after sitting switched off | The charger board drains slowly. Unplug the battery for storage |

Still stuck? [Open an issue](https://github.com/MakerSteele/non-zerobot/issues).
