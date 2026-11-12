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

## 3. Download the software onto the robot

Paste this whole line:

```
cd ~ && curl -L -o zerobot.zip https://github.com/MakerSteele/non-zerobot/releases/latest/download/zerobot.zip && rm -rf ~/zerobot && unzip -q zerobot.zip -d ~/zerobot && echo DOWNLOADED
```

You should see `DOWNLOADED` at the end. This gets the newest version from the [Releases page](https://github.com/MakerSteele/non-zerobot/releases).

## 4. Install it

Run these two, one at a time:

```
sudo bash ~/zerobot/install-root-helper.sh
```

```
bash ~/zerobot/install-service.sh
```

The second one installs everything the robot needs, so it can take 10+ minutes on a Pi Zero. Let it finish.

When you're done, reboot:

```
sudo reboot
```

## 5. Drive!

After about a minute, on your phone (same WiFi), open:

```
http://zerobot.local:5000
```

(Or use the robot's address instead of `zerobot.local`.)

**First things to try:**
- Drive forward a little. The **BAL** button changes to **BAL ✓** once the robot has confirmed each motor and encoder is wired the right way round. To run the check on purpose, open `http://zerobot.local:5000/paircheck`. The wheels will turn, so lift the robot first.
- Tap **TALK** to make the robot speak. The first time, your phone warns about the certificate. Tap **Advanced → Proceed**. That's normal: the robot makes its own certificate so your phone's microphone can be used.

---

## Updating to a new version

SSH in and run step 3 again, then:

```
sudo systemctl restart zerobot
```

Your tuning settings are stored outside the `zerobot` folder, so they survive updates.

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
