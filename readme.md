# MAVEN Development & Deployment Workflow

## Project Location

**Active MAVEN project directory:**

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

This is the only directory that should be used for development.

**Do NOT edit files inside:**

```bash
/home/mayankkonduri/MAVEN
/home/mayankkonduri/MAVEN_BACKUP
```

These directories are legacy backups from the Git corruption recovery and are not connected to the active system services.

---

# Making Code Changes

1. Open and edit files inside:

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

Example:

```bash
/home/mayankkonduri/MAVEN_CLEAN/voice_assistant.py
```

2. Save your changes.

---

# Restarting MAVEN Services

After modifying a file, restart the corresponding service so the Raspberry Pi loads the updated code.

## Voice Assistant

```bash
sudo systemctl restart maven-voice.service
```

## Camera Server

```bash
sudo systemctl restart maven-camera.service
```

## Microphone Server

```bash
sudo systemctl restart maven-mic.service
```

## IR Server

```bash
sudo systemctl restart maven.service
```

## Restart All MAVEN Services

```bash
sudo systemctl restart maven.service maven-camera.service maven-mic.service maven-voice.service
```

---

# Checking Service Status

Check all MAVEN services:

```bash
systemctl status maven.service maven-camera.service maven-mic.service maven-voice.service
```

---

# Viewing Logs

## Voice Assistant Logs

```bash
journalctl -u maven-voice.service -f
```

## Camera Server Logs

```bash
journalctl -u maven-camera.service -f
```

## Microphone Server Logs

```bash
journalctl -u maven-mic.service -f
```

## IR Server Logs

```bash
journalctl -u maven.service -f
```

Press `Ctrl + C` to exit the live log view.

---

# Saving Changes to GitHub

After your changes have been tested and verified:

```bash
cd ~/MAVEN_CLEAN

git status
git add .
git commit -m "Describe what you changed"
git push
```

This updates the GitHub repository:

```
MayankKonduri/MAVEN
```

---

# Important Notes

* The Raspberry Pi `systemd` services are configured to run code from:

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

* Editing files in the old `MAVEN` directory will have **no effect** on the running system.

* Always test changes locally by restarting the appropriate service before committing and pushing to GitHub.

* Keep `MAVEN_BACKUP` as a temporary safety copy until the new system has been fully validated.


---

# USB Microphone Troubleshooting & Calibration

If the USB microphone is unplugged and plugged back in, it may reset its audio settings. Common issues include:

* Auto Gain Control (AGC) turning back on
* Microphone gain returning to 100%
* MAVEN input levels becoming too sensitive

### Symptoms

* Idle noise is high (10–50%)
* MAVEN does not reliably detect wake words
* The input level meter constantly jumps without speaking

---

## 1. Verify the Correct USB Microphone

Before making any adjustments, make sure MAVEN is using the USB microphone and not the Raspberry Pi's built-in audio device.

Check available recording devices:

```bash
arecord -l
```

You should see something similar to:

```text
**** List of CAPTURE Hardware Devices ****
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
```

Open the USB microphone mixer directly:

```bash
alsamixer -c 1
```

Verify the top-left corner shows:

```text
Card: USB PnP Sound Device
Chip: USB Mixer
```

**Do not adjust:**

```text
Card: bcm2835 Headphones
Chip: Broadcom Mixer
```

because that is the Raspberry Pi audio output, not the MAVEN USB microphone.

---

## 2. Adjust USB Microphone Gain

Inside `alsamixer`:

1. Switch to **Capture** view (`F4`)
2. Navigate to the **Mic** control
3. Disable **Auto Gain Control (AGC)**
4. Lower the microphone gain

Recommended settings:

* Auto Gain Control: **OFF**
* Mic Gain: ~40–60%
* Avoid 100% / +23.81 dB gain

A good microphone level target:

| Condition                       | Desired Input Level |
| ------------------------------- | ------------------- |
| Silent room                     | 0–5%                |
| Background noise                | 5–10%               |
| Normal speech                   | 20–50%              |
| Loud speech close to microphone | 60–100%             |

---

## 3. Save ALSA Microphone Settings

After adjusting the microphone:

Exit `alsamixer` and run:

```bash
sudo alsactl store
```

This saves the USB microphone settings so they persist after reboot.

---

## 4. Restart MAVEN Audio Services

The microphone server and voice assistant need to reload the updated audio configuration:

```bash
sudo systemctl restart maven-mic.service
sudo systemctl restart maven-voice.service
```


---


## 5. Verify MAVEN Input Levels

Open the MAVEN microphone input level page and confirm:

* Quiet room: around 0–5%
* Normal ambient noise: 5–10%
* Saying "MAVEN": around 20–50%
* Loud speech close to the mic: 60–100%

If the idle level is still too high, return to:

```bash
alsamixer -c 1
```

and further reduce the microphone gain.

---

## Quick Recovery After Unplugging the USB Mic

If MAVEN stops recognizing your voice after unplugging/replugging the microphone:

```bash
# Verify USB microphone is detected
arecord -l

# Open USB microphone controls
alsamixer -c 1

# Save audio settings
sudo alsactl store

# Reload MAVEN microphone and voice services
sudo systemctl restart maven-mic.service
sudo systemctl restart maven-voice.service
```

This process restores the USB microphone configuration and reloads MAVEN with the updated audio settings.
