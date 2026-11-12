# Installing the Non-Zerobot software

This covers the software. Building the robot itself is in [ASSEMBLY.md](ASSEMBLY.md).

About 20–30 minutes, most of it waiting. You need a computer, a microSD card (16 GB or larger), and your assembled robot.

> **Tip:** every command below is one line. Copy it, paste it into the terminal, press Enter, and wait for it to finish before the next one.

---

## 1. Put Raspberry Pi OS on the card

1. Install **Raspberry Pi Imager** from [raspberrypi.com/software](https://www.raspberrypi.com/software/).
2. Choose your Pi model, then **Raspberry Pi OS (other) → Raspberry Pi OS Lite (32-bit)**. Use the 32-bit Lite version on every Pi: 64-bit won't start on a Pi Zero W, and the desktop is too heavy for the robot.
3. When Imager offers to **edit settings**, say yes and fill in:
   - **Hostname** — e.g. `zerobot`
   - **Username and password** — anything you like. Write them down.
   - **WiFi** — your phone's hotspot or home WiFi (name and password)
   - **Services tab** → turn on **SSH** with password login
4. Write the card, put it in the robot, and switch it on. The first boot takes a few minutes.

## 2. Connect to the robot

From a computer on the same WiFi, open a terminal (PowerShell on Windows) and type, using your own username and hostname:

```
ssh yourname@zerobot.local
```

Type `yes` if it asks about a fingerprint, then your password.

> **Can't connect?** Give it another minute and try again. If `zerobot.local` still isn't found, look up the robot's address in your phone hotspot's "connected devices" list and use that instead, e.g. `ssh yourname@192.168.1.50`.

## 3. Install the software

Paste this one line and press Enter:

```
curl -fsSL https://raw.githubusercontent.com/MakerSteele/non-zerobot/main/install.sh | bash
```

It checks your Pi first (right OS, internet, enough space) and stops with a plain explanation if something's wrong. Then it asks for your password once, downloads the newest software from the [Releases page](https://github.com/MakerSteele/non-zerobot/releases), and installs everything. That takes several minutes, longer on a Pi Zero.

At the end it shows the robot's address **and a QR code**. Point your phone's camera at the QR code to open the control page. Then press Enter to restart the robot.

## 4. Drive!

After about a minute, scan the QR code from step 3, or open this on your phone (same WiFi):

```
http://zerobot.local:5000
```

(Or use the robot's address instead of `zerobot.local`.)

**First things to try:**
- Open the **⚙** menu and tap **CHECK**. It's a red/green list of everything that can be wired or set up wrong, with what to do for anything red. Keep it open while you drive a little and run its pair check, until everything's green. (Until it passes once, the robot starts at a gentle 40% speed.)
- Drive forward a little. The **BAL** button changes to **BAL ✓** once the robot has confirmed each motor and encoder is wired the right way round. To run the check on purpose, open `http://zerobot.local:5000/paircheck`. The wheels will turn, so lift the robot first.
- Tap **TALK** to make the robot speak. The first time, your phone warns about the certificate. Tap **Advanced → Proceed**. That's normal: the robot makes its own certificate so your phone's microphone can be used.

---

## Updating to a new version

Run the same one line from step 3 again. Your tuning settings are stored outside the `zerobot` folder, so they're kept, and the previous version is saved in `~/zerobot.old`.

## Handy commands

| What | Command |
|---|---|
| Watch the robot's log | `journalctl -u zerobot -f` |
| Restart the software | `sudo systemctl restart zerobot` |
| Stop it | `sudo systemctl stop zerobot` |
| Find which pin an LED is on | `python3 ~/zerobot/findpin.py` |

## Optional: robot's own WiFi network

The robot can host its own WiFi network (`ZeroBot`, password `zerobot1`, address `10.42.0.1`) for places with no WiFi. Change the password in `setup-ap.sh` first, then:

```
sudo bash ~/zerobot/setup-ap.sh
sudo netmode ap
```

Your phone loses internet while it's connected to the robot's network. `sudo netmode client` switches back.

## Something not working?

[Open an issue](https://github.com/MakerSteele/non-zerobot/issues) and paste in the output of `journalctl -u zerobot -n 50 --no-pager`.
