<div align="center">

# 🎙️ MAVEN

### Voice + Gesture Controlled TV Assistant for Raspberry Pi

<p>
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Raspberry_Pi-Deployed-A22846?style=for-the-badge&logo=raspberrypi&logoColor=white" alt="Raspberry Pi" />
  <img src="https://img.shields.io/badge/Flask-Servers-000000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask" />
  <img src="https://img.shields.io/badge/ONNX-Runtime-005CED?style=for-the-badge&logo=onnx&logoColor=white" alt="ONNX" />
</p>
 
<p>
  <img src="https://img.shields.io/badge/STT-faster--whisper_base.en-4B8BBE?style=flat-square" alt="Whisper" />
  <img src="https://img.shields.io/badge/vision-YOLOv8n_+_handpose-00A67E?style=flat-square" alt="Vision" />
  <img src="https://img.shields.io/badge/IR-38kHz_pigpio-FF6B35?style=flat-square" alt="IR" />
  <img src="https://img.shields.io/badge/services-4_systemd_units-yellow?style=flat-square" alt="Services" />
</p>

</div>

---

## 📖 What MAVEN Is

MAVEN turns any IR-controlled TV into a hands-free device. It runs entirely **on-device** on a Raspberry Pi — no cloud, no external API calls. Speech is transcribed locally with `faster-whisper`, hand gestures are recognized locally with ONNX vision models, and both paths converge on a single IR blaster that replays learned remote codes.

<table>
<tr>
<td width="55"><h3 align="center">🗣️</h3></td>
<td><b>Voice</b><br>Say a wake word, then speak commands in natural language for five minutes.</td>
</tr>
<tr>
<td width="55"><h3 align="center">✋</h3></td>
<td><b>Gesture</b><br>Hold an open palm, thumbs up, or thumbs down in front of the camera.</td>
</tr>
<tr>
<td width="55"><h3 align="center">📡</h3></td>
<td><b>IR Replay</b><br>Learned pulse trains stored in SQLite, retransmitted on a 38 kHz carrier.</td>
</tr>
<tr>
<td width="55"><h3 align="center">💡</h3></td>
<td><b>LED Feedback</b><br>Green = listening/active, red = command fired.</td>
</tr>
</table>

---

## 🏗️ Architecture

MAVEN is four independent processes that talk to each other over `localhost` HTTP. Each one runs as its own `systemd` unit, so any single component can be restarted without taking down the rest.

```
        ┌──────────────────┐          ┌──────────────────┐
        │  microphone_     │          │  camera_server   │
        │  server.py :8082 │          │      :8081       │
        │                  │          │                  │
        │  USB mic →       │          │  Picamera2 →     │
        │  10s rolling     │          │  640x480 @12fps  │
        │  PCM buffer      │          │  MJPEG frames    │
        └────────┬─────────┘          └─────────┬────────┘
                 │ /level, /recent_5s.wav       │ /frame.jpg
                 ▼                              ▼
        ┌──────────────────┐          ┌──────────────────┐
        │ voice_assistant  │◄────────►│ camera_assistant │
        │   state :8083    │  state   │   state :8084    │
        │                  │  swap    │                  │
        │ VAD → Whisper →  │          │ YOLOv8n person → │
        │ regex intent     │          │ hand → landmarks │
        └────────┬─────────┘          └─────────┬────────┘
                 │      POST /api/send/<button> │
                 │      X-Maven-Token: <token>  │
                 └───────────────┬──────────────┘
                                 ▼
                     ┌───────────────────────┐
                     │   pi_server.py :5000  │
                     │                       │
                     │  SQLite ir_codes.db   │
                     │  → pigpio waveform    │
                     │  → IR LED (GPIO 17)   │
                     └───────────────────────┘
                                 ▼
                              📺 TV
```

### Service & Port Map

| Service | File | Port | Responsibility |
| :--- | :--- | :---: | :--- |
| **IR Server** | `pi_server.py` | `5000` | IR learn/send, SQLite codes, session tokens, pairing |
| **Camera Server** | `camera_server.py` | `8081` | Picamera2 capture, JPEG frames, MJPEG stream |
| **Microphone Server** | `microphone_server.py` | `8082` | Rolling 10s audio buffer, RMS levels, WAV export |
| **Voice Assistant** | `voice_assistant.py` | `8083` | VAD, Whisper STT, wake words, intent → IR |
| **Camera Assistant** | `camera_assistant.py` | `8084` | Person + hand detection, gesture → IR |

> [!NOTE]
> `8083` and `8084` serve only a tiny `/state` JSON endpoint. The two assistants poll each other so a gesture doesn't fire while a voice command is mid-flight.

---

## 📂 Repository Layout

```
MAVEN/
├── pi_server.py                 # IR server — Flask, pigpio, SQLite, web UI
├── camera_server.py             # Picamera2 → MJPEG/JPEG endpoints
├── microphone_server.py         # DVR-style rolling audio buffer + dashboard
├── voice_assistant.py           # wake word → Whisper → intent → IR
├── camera_assistant.py          # YOLOv8n → hand → landmarks → gesture → IR
│
├── ir_codes.db                  # SQLite: learned IR pulse trains
├── models/
│   ├── yolov8n.onnx             # person detection      (~12 MB)
│   ├── hand_yolov8n.onnx        # hand detection        (~12 MB)
│   └── handpose_estimation.onnx # 21-point landmarks    (~4 MB)
│
├── assets/
│   └── startup-school-2026-venkata-naga-mayank-konduri.png  # Startup School 2026 certificate
│
├── TV_Control_Original/         # standalone prototypes (pre-integration)
│   ├── TV_Control_Learn_IRCodes.py
│   ├── TV_Control_Use_IRCodes.py
│   └── TV_Control_Predefined_IRCodes.py
│
├── keyboard_test_token.py       # manual IR button tester (CLI)
├── test_landmarks.py            # hand landmark visualizer
├── test.py                      # trivial connectivity check
├── debug_camera_assistant.jpg   # last annotated gesture frame
└── debug_landmarks.jpg          # last annotated landmark frame
```

---

## 🔌 Hardware & GPIO

<div align="center">

| GPIO | Component | Direction | Notes |
| :---: | :--- | :---: | :--- |
| **17** | IR transmit LED | OUT | 38 kHz carrier, 33% duty |
| **22** | Red status LED | OUT | Flashes when an IR code is sent |
| **23** | Green listen LED | OUT | Solid/blinking during active command mode |
| **24** | Pairing button | IN (pull-down) | Hold ~3–5s to enter discovery mode |
| **27** | IR receive sensor | IN (pull-up) | Used during code learning |

</div>

**Also required:** USB microphone (`USB PnP Sound Device`), Raspberry Pi Camera module, and a running `pigpiod` daemon.

<details>
<summary><b>📦 Python dependencies</b></summary>

<br>

```bash
pip install flask flask-cors requests pigpio pyaudio \
            faster-whisper onnxruntime opencv-python numpy
```

`picamera2` ships with Raspberry Pi OS — install via `sudo apt install -y python3-picamera2` rather than pip.

> [!TIP]
> There is no `requirements.txt` in the repo yet. Freezing one from the working Pi would make redeploys far less painful:
> ```bash
> pip freeze > requirements.txt
> ```

</details>

---

## 🗣️ Voice Commands

Say a wake word, wait for the **green LED**, then issue commands. Command mode stays open for **5 minutes** and can be exited early by saying *"bye"*, *"goodbye"*, or *"stop listening"*.

**Wake words:** `maven` — plus ~20 phonetic variants the transcriber commonly produces (*mavin, mayven, raven, haven, nathan*), and prefixed forms like *"hey maven"* / *"okay maven"*.

<div align="center">

| Say | Action | IR Button |
| :--- | :--- | :--- |
| "power" · "turn it on/off" | Toggle power | `power_toggle` |
| "volume up" · "turn up the volume" | Volume up | `vol_up` |
| "volume down" · "quieter" | Volume down | `vol_down` |
| "mute" · "unmute" | Toggle mute | `mute` |
| "channel up" · "next channel" | Channel up | `channel_up` |
| "channel down" · "previous channel" | Channel down | `channel_down` |
| "home" · "menu" | Home screen | `home` |
| "up" · "down" · "left" · "right" | D-pad navigation | `up` `down` `left` `right` |
| "select" · "enter" · "ok" | Confirm | `enter` |
| "back" · "return" · "exit" | Go back | `return` |

</div>

> [!NOTE]
> Bare **"up"** and **"down"** are navigation. Volume requires the word *"volume"* explicitly — this disambiguation is intentional.

<details>
<summary><b>⚙️ Voice tuning constants</b> <code>voice_assistant.py</code></summary>

<br>

| Constant | Default | Meaning |
| :--- | :---: | :--- |
| `VOICE_ACTIVITY_THRESHOLD` | `12.0` | RMS % that counts as speech |
| `SILENCE_AFTER_SPEECH_SECONDS` | `2.5` | Stable silence before transcribing |
| `ACTIVE_MODE_SECONDS` | `300` | How long command mode stays open |
| `WAKE_COOLDOWN` | `2.0` | Minimum gap between wake detections |
| `WAKE_FLUSH_SECONDS` | `1.5` | Buffer flush so the wake word doesn't leak into the command |
| `WHISPER_TIMEOUT_SECONDS` | `20.0` | Abort a stuck transcription |
| `WHISPER_MODEL` | `base.en` | Swap to `tiny.en` for speed, `small.en` for accuracy |

</details>

---

## ✋ Gesture Commands

Hold a gesture steady for **~2.3 seconds** near the center of frame. A **2.5s cooldown** follows each trigger.

<div align="center">

| Gesture | Action | IR Button |
| :---: | :--- | :--- |
| 🖐️ **Open palm** | Toggle power | `power_toggle` |
| 👍 **Thumbs up** | Volume up | `vol_up` |
| 👎 **Thumbs down** | Volume down | `vol_down` |

</div>

The pipeline runs at ~2 Hz: YOLOv8n confirms a person, a second detector finds the hand, and the handpose model returns 21 landmarks that are classified by finger extension. Detections are smoothed over a 5-frame window requiring 3 agreements before acting.

---

## ✏️ Making Code Changes

1. Open and edit files inside `/home/mayankkonduri/MAVEN_CLEAN` — for example:

```bash
/home/mayankkonduri/MAVEN_CLEAN/voice_assistant.py
```

2. Save your changes.
3. Restart the matching service (below) so the Pi loads the updated code.

---

## 🔄 Restarting MAVEN Services

<div align="center">

| Component | Command |
| :--- | :--- |
| Voice Assistant | `sudo systemctl restart maven-voice.service` |
| Camera Server | `sudo systemctl restart maven-camera.service` |
| Microphone Server | `sudo systemctl restart maven-mic.service` |
| IR Server | `sudo systemctl restart maven.service` |

</div>

**Restart everything:**

```bash
sudo systemctl restart maven.service maven-camera.service maven-mic.service maven-voice.service
```

> [!IMPORTANT]
> `pi_server.py` mints a **fresh session token on every start**. If you restart the IR server, the assistants still hold the old token and will get `403` on every send — restart them too.

---

## 📊 Checking Service Status

```bash
systemctl status maven.service maven-camera.service maven-mic.service maven-voice.service
```

---

## 📜 Viewing Logs

<div align="center">

| Component | Command |
| :--- | :--- |
| Voice Assistant | `journalctl -u maven-voice.service -f` |
| Camera Server | `journalctl -u maven-camera.service -f` |
| Microphone Server | `journalctl -u maven-mic.service -f` |
| IR Server | `journalctl -u maven.service -f` |

</div>

Press `Ctrl + C` to exit the live log view.

---

## 🌐 Web Dashboards

Every server exposes a browser UI — useful for diagnosing a problem without reading logs.

<div align="center">

| Dashboard | URL | What it shows |
| :--- | :--- | :--- |
| IR Control Panel | `http://<pi-ip>:5000/` | Learn and fire each button |
| Camera Stream | `http://<pi-ip>:8081/` | Live MJPEG view |
| Microphone Meter | `http://<pi-ip>:8082/` | Live input level + audio playback |

</div>

---

## 📤 Saving Changes to GitHub

After your changes have been tested and verified:

```bash
cd ~/MAVEN_CLEAN

git status
git add .
git commit -m "Describe what you changed"
git push
```

This updates the repository: **[MayankKonduri/MAVEN](https://github.com/MayankKonduri/MAVEN)**

---

## 📌 Important Notes

- The Raspberry Pi `systemd` services run code from `/home/mayankkonduri/MAVEN_CLEAN`.
- Editing files in the old `MAVEN` directory will have **no effect** on the running system.
- Always test changes locally by restarting the appropriate service before committing and pushing.
- Keep `MAVEN_BACKUP` as a temporary safety copy until the new system has been fully validated.

---

## 🏅 Recognition

<div align="center">

<img width="(2064/2)" height="(1560/2)" alt="startup-school-2026-venkata-naga-mayank-konduri" src="https://github.com/user-attachments/assets/6f71b44c-f507-442d-8e69-adf505ba1644" />

</div>

---

<div align="center">

<sub>Built for Raspberry Pi · 100% on-device inference · No cloud dependencies</sub>

</div>
