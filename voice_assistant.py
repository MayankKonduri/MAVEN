#!/usr/bin/env python3
"""
MAVEN Voice Assistant with Whisper + Command Execution
Polls microphone_server, detects wake words, listens for commands, executes via IR.

Workflow:
  1. Continuously poll /level
  2. When RMS crosses VOICE_ACTIVITY_THRESHOLD, note it and keep polling
  3. When RMS drops back below threshold, wait SILENCE_AFTER_SPEECH_SECONDS (0.5s)
     — if RMS rises again during that window, treat it as continued speech and keep waiting
  4. Once a full 0.5s of silence has elapsed, fetch /recent_5s.wav and run Whisper
  5. Check for wake words (IDLE mode) or parse command (ACTIVE mode)
  6. On wake word match: flush stale audio buffer, enter ACTIVE_COMMAND mode for 5 minutes
  7. In ACTIVE_COMMAND mode: same VAD loop, no wake word required
  8. After 5 minutes of inactivity, return to wake word detection

Install: pip install faster-whisper requests
Run: python3 voice_assistant.py
"""

import requests
from faster_whisper import WhisperModel
import time
import re
import tempfile
import os
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

# ── pigpio for LED control ─────────────────────────────────────────────────────
# Green LED on GPIO 23 lights up when command mode is active.
# Graceful fallback: if pigpiod is not running the assistant still works,
# just without LED feedback.
try:
    import pigpio
    pi = pigpio.pi()
    if not pi.connected:
        pi = None
except Exception:
    pi = None

# ── Configuration ──────────────────────────────────────────────────────────────

MIC_SERVER_URL = "http://localhost:8082"
IR_SERVER_URL = "http://localhost:5000"
CAMERA_ASSISTANT_URL = "http://localhost:8083"

WHISPER_MODEL = "base.en"

GREEN_LED_PIN = 23 # GPIO pin number for command-mode indicator LED

WAKE_WORDS = [
    "maven",
    "mayven",
    "mavin",
    "mavon",
    "mavun",
    "mave",
    "naven",
    "navin",
    "nathan",
    "raven",
    "haven",
    "hey maven",
    "hi maven",
    "hello maven",
    "okay maven",
    "ok maven",
    "hey mayven",
    "hi mayven",
    "hello mayven",
    "okay mayven",
    "ok mayven",
    "hey me even",
    "hi me even",
    "hello me even",
    "hello",
]

# Phrases that exit ACTIVE command mode and return to wake word detection.
GOODBYE_WORDS = [
    "bye",
    "goodbye",
    "good bye",
    "exit command mode",
    "stop listening",
]

ACTIVE_MODE_SECONDS = 300 # stay awake for 5 minutes after wake word
VOICE_ACTIVITY_THRESHOLD = 12.0 # RMS % to trigger STT
SILENCE_AFTER_SPEECH_SECONDS = 2.5 # stable silence required before transcribing
SILENCE_POLL_INTERVAL = 0.05 # seconds between RMS checks while waiting for silence
POLLING_INTERVAL = 0.1 # seconds between level checks in main loop
WAKE_COOLDOWN = 2.0 # don't re-detect wake word within this time
STATUS_LOG_INTERVAL = 4.0 # print state banner no faster than this
WHISPER_TIMEOUT_SECONDS = 20.0 # abort transcription if it takes longer than this

# Seconds of silence to flush the stale buffer after wake word is detected.
# Prevents the wake phrase from leaking into the first command clip.
WAKE_FLUSH_SECONDS = 1.5

# ── LED state ──────────────────────────────────────────────────────────────────

led_blink_active = False
led_blink_stop = False

def led_on():
    """Turn the green command-mode LED on (solid)."""
    global led_blink_active, led_blink_stop
    led_blink_active = False
    led_blink_stop = True
    if pi:
        pi.write(GREEN_LED_PIN, 1)

def led_off():
    """Turn the green command-mode LED off."""
    global led_blink_active, led_blink_stop
    led_blink_active = False
    led_blink_stop = True
    if pi:
        pi.write(GREEN_LED_PIN, 0)

def led_blink():
    """Blink the green command-mode LED."""
    global led_blink_active, led_blink_stop
    led_blink_stop = False
    if led_blink_active:
        return
    led_blink_active = True
    
    def _blink():
        while not led_blink_stop:
            if pi:
                pi.write(GREEN_LED_PIN, 1)
            time.sleep(0.3)
            if pi:
                pi.write(GREEN_LED_PIN, 0)
            time.sleep(0.3)
    
    t = threading.Thread(target=_blink, daemon=True)
    t.start()

# ── State Publishing ───────────────────────────────────────────────────────────

voice_state = {"state": "idle"}
voice_state_lock = threading.Lock()

class StateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with voice_state_lock:
                self.wfile.write(json.dumps(voice_state).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass

def start_state_server():
    server = HTTPServer(("localhost", 8083), StateHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

def get_camera_state():
    """Fetch person_present state from camera_assistant"""
    try:
        r = requests.get(f"{CAMERA_ASSISTANT_URL}/state", timeout=1)
        if r.status_code == 200:
            return r.json().get("person_present", False)
    except Exception:
        pass
    return False

# ── IR Token ──────────────────────────────────────────────────────────────────


def load_ir_token() -> str:
    """Fetch session token directly from pi_server (same Pi, local only)."""
    try:
        r = requests.get(f"{IR_SERVER_URL}/api/local/token", timeout=3)
        if r.status_code == 200:
            return r.json().get("token", "")
    except requests.exceptions.RequestException:
        pass
    return ""

IR_TOKEN = load_ir_token()

# ── Command Mappings ──────────────────────────────────────────────────────────
# Map natural language patterns to IR button names.
# Note: navigation "up"/"down" are disambiguated from volume by requiring the
# word "volume" for volume commands, so bare "up" or "down" becomes navigation.

COMMAND_PATTERNS = [
    # ── Power ────────────────────────────────────────────────────────────────
    (r"\b(power|turn)\s+(on|off)\b", "power_toggle"),
    (r"\bturn\s+(it\s+)?(on|off)\b", "power_toggle"),
    # common tiny.en / base.en mishearings of "power"
    (r"\b(hour|ower|powder)\s+(on|off)\b", "power_toggle"),
    (r"\bpower\b", "power_toggle"),

    # ── Volume ───────────────────────────────────────────────────────────────
    (r"\bvolume\s+(up|louder|higher)\b", "vol_up"),
    (r"\b(increase|turn\s+up)\s+(the\s+)?volume\b", "vol_up"),
    (r"\bvolume\s+(down|quieter|lower)\b", "vol_down"),
    (r"\b(decrease|turn\s+down)\s+(the\s+)?volume\b", "vol_down"),

    # ── Mute ─────────────────────────────────────────────────────────────────
    (r"\b(mute|unmute)\b", "mute"),

    # ── Channels ─────────────────────────────────────────────────────────────
    (r"\bchannel\s+(up|next)\b", "channel_up"),
    (r"\bnext\s+channel\b", "channel_up"),
    (r"\bchannel\s+(down|previous|back)\b", "channel_down"),
    (r"\bprevious\s+channel\b", "channel_down"),

    # ── Navigation ────────────────────────────────────────────────────────────
    # "up" / "down" as bare words → navigation (not volume)
    (r"\b(go\s+)?(home|menu)\b", "home"),
    (r"\bleft\b", "left"),
    (r"\bright\b", "right"),
    (r"\bup\b", "up"),
    (r"\bdown\b", "down"),
    (r"\b(select|enter|ok|okay)\b", "enter"),
    (r"\b(back|return|exit)\b", "return"),
]

# ── Logging ────────────────────────────────────────────────────────────────────

class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3

LOG_LEVEL = LogLevel.INFO

def log(level: LogLevel, msg: str):
    """Structured logging"""
    if level.value < LOG_LEVEL.value:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {
        LogLevel.DEBUG: "🔍",
        LogLevel.INFO: "📡",
        LogLevel.WARN: "⚠️ ",
        LogLevel.ERROR: "❌",
    }[level]
    print(f"[{timestamp}] {prefix} {msg}", flush=True)

# ── Microphone Server Interface ────────────────────────────────────────────────

def get_current_level():
    """Poll /level endpoint to get RMS percentage"""
    try:
        r = requests.get(f"{MIC_SERVER_URL}/level", timeout=2)
        if r.status_code == 200:
            return r.json()
    except requests.exceptions.RequestException as e:
        log(LogLevel.ERROR, f"Level fetch failed: {e}")
    return {"rms_pct": 0, "ok": False}

def get_recent_audio():
    """Fetch last 5 seconds of audio as WAV bytes from /recent_5s.wav"""
    try:
        r = requests.get(f"{MIC_SERVER_URL}/recent_5s.wav", timeout=3)
        if r.status_code == 200:
            log(LogLevel.DEBUG, f"Audio fetched: {len(r.content)} bytes")
            return r.content
        log(LogLevel.ERROR, f"Audio fetch HTTP {r.status_code}: {r.text[:120]}")
    except requests.exceptions.RequestException as e:
        log(LogLevel.ERROR, f"Audio fetch failed: {e}")
    return None

# ── IR Server Interface ────────────────────────────────────────────────────────

def ir_send_command(button_name):
    """
    Send IR command to pi_server.
    pi_server expects:
      POST /api/send/<name>
      Header: X-Maven-Token: <token>
    pi_server then does:
      SELECT pulses FROM ir_codes WHERE name=? ← pulse data from SQLite DB
      send_stored(pi, stored_pulses) ← replays the IR signal
      flash_led(pi, STATUS_LED_PIN)
    """
    if not IR_TOKEN:
        log(LogLevel.ERROR, "No IR token — cannot send command.")
        log(LogLevel.ERROR,
            "Add /api/local/token route to pi_server.py (see comment at top of file).")
        return False

    try:
        headers = {"X-Maven-Token": IR_TOKEN}
        r = requests.post(
            f"{IR_SERVER_URL}/api/send/{button_name}",
            headers=headers,
            timeout=3,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                log(LogLevel.INFO, f"✅ IR command sent: {button_name}")
                return True
            log(LogLevel.WARN, f"IR send rejected: {data}")
        elif r.status_code == 403:
            log(LogLevel.ERROR,
                "IR token rejected (403) — pi_server may have restarted. "
                "Restart voice_assistant.py to fetch a fresh token.")
        elif r.status_code == 404:
            log(LogLevel.WARN,
                f"Button '{button_name}' not learned yet in pi_server DB.")
        else:
            log(LogLevel.WARN, f"IR send HTTP {r.status_code} for: {button_name}")
    except requests.exceptions.RequestException as e:
        log(LogLevel.ERROR, f"IR send error: {e}")

    return False

# ── Speech Recognition (Whisper) ───────────────────────────────────────────────

class WhisperTranscriber:
    def __init__(self, model_name=WHISPER_MODEL):
        log(LogLevel.INFO, f"Loading Whisper model '{model_name}'...")
        try:
            self.model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
            )
            log(LogLevel.INFO, "✓ Whisper ready")
        except Exception as e:
            log(LogLevel.ERROR, f"Failed to load Whisper: {e}")
            raise

    def transcribe(self, audio_bytes, label=""):
        """
        Convert WAV bytes to text using Whisper.
        Runs in a daemon thread so we can enforce WHISPER_TIMEOUT_SECONDS.
        Returns (text, success) tuple — (None, False) on timeout or error.
        """
        if not audio_bytes:
            return None, False

        result_holder = [None, False]
        exception_holder = [None]

        def _run():
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(audio_bytes)
                    temp_path = f.name
                try:
                    log(LogLevel.DEBUG, f"Transcribing {label}...")
                    segments, info = self.model.transcribe(
                        temp_path,
                        language="en",
                        beam_size=1,
                        vad_filter=True,
                    )
                    text = " ".join(seg.text for seg in segments).strip().lower()
                    if text:
                        result_holder[0] = text
                        result_holder[1] = True
                finally:
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
            except Exception as e:
                exception_holder[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=WHISPER_TIMEOUT_SECONDS)

        if t.is_alive():
            log(LogLevel.ERROR,
                f"Whisper timed out after {WHISPER_TIMEOUT_SECONDS}s {label} — "
                "discarding audio, returning to listening")
            return None, False

        if exception_holder[0]:
            log(LogLevel.ERROR, f"Whisper error {label}: {exception_holder[0]}")
            return None, False

        return result_holder[0], result_holder[1]

transcriber = None

def init_whisper():
    """Initialize Whisper model (blocking)"""
    global transcriber
    transcriber = WhisperTranscriber(WHISPER_MODEL)

# ── Text Normalization ─────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace"""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_wake_word(text: str) -> bool:
    """Return True if any wake word appears in the normalized text"""
    text = normalize_text(text)
    for wake in WAKE_WORDS:
        if normalize_text(wake) in text:
            return True
    return False

def is_goodbye(text: str) -> bool:
    """Return True if the text contains a goodbye/exit phrase."""
    text = normalize_text(text)
    for phrase in GOODBYE_WORDS:
        if normalize_text(phrase) in text:
            return True
    return False

def strip_wake_words(text: str) -> str:
    """Remove all wake word variants from text"""
    text = normalize_text(text)
    # Sort by length descending so longer phrases ("hey maven") match first
    for wake in sorted(WAKE_WORDS, key=len, reverse=True):
        text = text.replace(normalize_text(wake), " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ── Command Parsing ───────────────────────────────────────────────────────────

def parse_command(raw_text: str):
    """
    Clean the command text, then match against COMMAND_PATTERNS.
    Returns (button_name, cleaned_text) or (None, cleaned_text).
    """
    cleaned = strip_wake_words(raw_text)
    log(LogLevel.INFO, f'Command text after cleanup: "{cleaned}"')

    for pattern, button in COMMAND_PATTERNS:
        if re.search(pattern, cleaned):
            log(LogLevel.DEBUG, f"Pattern '{pattern}' matched → {button}")
            return button, cleaned

    return None, cleaned

# ── Voice Activity Detection ───────────────────────────────────────────────────

def wait_for_speech_to_finish():
    """
    Called right after the first RMS threshold crossing.

    Blocks until the speaker has been continuously silent for
    SILENCE_AFTER_SPEECH_SECONDS. Any RMS blip back above the threshold
    resets the silence clock so mid-sentence pauses do not cut off the
    utterance.

    Uses wall-clock time throughout so HTTP poll latency never causes
    a premature exit.
    """
    # We know voice just started. Sleep briefly so we do not immediately
    # see a below-threshold reading before the speaker has said anything.
    time.sleep(0.15)

    in_silence = False
    silence_start = None

    while True:
        level = get_current_level()
        rms_pct = level.get("rms_pct", 0)

        if rms_pct >= VOICE_ACTIVITY_THRESHOLD:
            # Still speaking (or resumed) -- reset silence state
            if in_silence:
                in_silence = False
                silence_start = None
        else:
            # Below threshold
            if not in_silence:
                in_silence = True
                silence_start = time.time()
                log(LogLevel.INFO, "Voice dropped below threshold -- waiting for silence...")
            elif time.time() - silence_start >= SILENCE_AFTER_SPEECH_SECONDS:
                return # silence confirmed -- proceed to transcribe

        time.sleep(SILENCE_POLL_INTERVAL)

# ── State Machine ──────────────────────────────────────────────────────────────

class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"

class VoiceAssistant:
    def __init__(self):
        self.state = State.IDLE
        self.last_wake_time = 0.0
        self.active_until = 0.0

        # Processing lock — True while waiting for silence or running Whisper;
        # prevents re-entry from a concurrent poll tick.
        self._processing = False

        # Throttle the status banner
        self._last_status_log = 0.0

        # Ensure LED starts off on boot
        led_off()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        """Main event loop — synchronous, no threads"""
        log(LogLevel.INFO, "Voice Assistant starting")
        log(LogLevel.INFO, f"Wake words include: {', '.join(WAKE_WORDS[:6])}…")
        log(LogLevel.INFO, f"Voice threshold: {VOICE_ACTIVITY_THRESHOLD}% RMS")
        if pi:
            log(LogLevel.INFO, f"Green LED ready on GPIO{GREEN_LED_PIN}")
        else:
            log(LogLevel.WARN, "pigpio not connected — LED control disabled")
        print()

        try:
            while True:
                if self.state == State.IDLE:
                    self._idle_tick()
                else:
                    self._active_tick()
                time.sleep(POLLING_INTERVAL)

        except KeyboardInterrupt:
            print()
            log(LogLevel.INFO, "Shutting down…")
        except Exception as e:
            log(LogLevel.ERROR, f"Fatal: {e}")
            raise
        finally:
            # Always turn LED off on exit so it doesn't stay lit under systemctl restart
            led_off()
            if pi:
                pi.stop()

    # ── IDLE ──────────────────────────────────────────────────────────────────

    def _idle_tick(self):
        """One polling tick in IDLE (wake word detection) mode"""

        now = time.time()

        # Print status banner once per STATUS_LOG_INTERVAL seconds
        if now - self._last_status_log >= STATUS_LOG_INTERVAL:
            log(LogLevel.INFO, "Listening for wake word…")
            self._last_status_log = now

        # Hard gate: skip if already processing
        if self._processing:
            return

        # Wake word cooldown
        if now - self.last_wake_time < WAKE_COOLDOWN:
            return

        level = get_current_level()
        rms_pct = level.get("rms_pct", 0)

        if rms_pct < VOICE_ACTIVITY_THRESHOLD:
            return

        # ── Voice detected — acquire lock and wait for speech to end ─────────
        self._processing = True
        log(LogLevel.INFO, f"Voice detected ({rms_pct:.1f}% RMS)…")

        wait_for_speech_to_finish()

        audio = get_recent_audio()
        if not audio:
            self._processing = False
            return

        log(LogLevel.INFO, "Processing input…")
        text, ok = transcriber.transcribe(audio, "[wake-detect]")
        self._processing = False

        if not ok or not text:
            return

        log(LogLevel.INFO, f'Heard: "{text}"')

        if is_wake_word(text):
            log(LogLevel.INFO, "🎤 Wake word detected!")
            self.last_wake_time = time.time()
            self._enter_active_mode()

    # ── ACTIVE ────────────────────────────────────────────────────────────────

    def _active_tick(self):
        """One polling tick in ACTIVE (command listening) mode"""

        # Check timeout
        now = time.time()
        if now >= self.active_until:
            self._exit_active_mode("Timed out — returning to wake word detection")
            return

        # Print status banner once per STATUS_LOG_INTERVAL seconds
        if now - self._last_status_log >= STATUS_LOG_INTERVAL:
            log(LogLevel.INFO, "Listening for command…")
            self._last_status_log = now

        # Hard gate: skip if already processing
        if self._processing:
            return

        level = get_current_level()
        rms_pct = level.get("rms_pct", 0)

        if rms_pct < VOICE_ACTIVITY_THRESHOLD:
            return

        # ── Voice detected — acquire lock and wait for speech to end ─────────
        self._processing = True
        log(LogLevel.INFO, f"Voice detected ({rms_pct:.1f}% RMS)…")

        wait_for_speech_to_finish()

        audio = get_recent_audio()
        if not audio:
            self._processing = False
            return

        log(LogLevel.INFO, "Processing command…")
        text, ok = transcriber.transcribe(audio, "[command]")
        self._processing = False

        if not ok or not text:
            log(LogLevel.WARN, "No speech detected (or timed out) — continuing command mode")
            return

        log(LogLevel.INFO, f'Heard: "{text}"')

        # ── Check for goodbye before attempting IR command ────────────────────
        if is_goodbye(text):
            self._exit_active_mode("Goodbye — returning to wake word detection")
            return

        self._execute_command(text)

        # Reset 5-minute timer on any voice activity (valid or not)
        self.active_until = time.time() + ACTIVE_MODE_SECONDS

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _enter_active_mode(self):
        """
        Flush stale audio buffer, turn LED on (blinking), then enter 5-minute ACTIVE_COMMAND mode.
        The flush pause ensures the wake phrase doesn't bleed into the first
        command clip.
        """
        log(LogLevel.INFO,
            f"Entering command mode — active for {ACTIVE_MODE_SECONDS // 60} minutes")
        log(LogLevel.DEBUG, f"Flushing stale buffer ({WAKE_FLUSH_SECONDS}s)…")

        # Short silence: let the mic buffer roll forward past the wake phrase
        time.sleep(WAKE_FLUSH_SECONDS)

        led_blink() # green LED blinking while in command mode

        self.state = State.ACTIVE
        self.active_until = time.time() + ACTIVE_MODE_SECONDS
        self._last_status_log = 0.0

        with voice_state_lock:
            voice_state["state"] = "active"

    def _exit_active_mode(self, reason: str):
        """
        Turn LED off or set to solid based on camera state, then return to IDLE (wake word) mode.
        Called on timeout or when a goodbye phrase is detected.
        """
        log(LogLevel.INFO, reason)
        
        # Check if person is still in frame
        camera_has_person = get_camera_state()
        
        if camera_has_person:
            led_on() # solid green if person still in frame
        else:
            led_off() # off if person left
        
        self.state = State.IDLE
        self._last_status_log = 0.0

        with voice_state_lock:
            voice_state["state"] = "idle"

    def _execute_command(self, raw_text: str):
        """Parse command text and fire IR if matched"""
        button, cleaned = parse_command(raw_text)

        if button:
            log(LogLevel.INFO, f"Matched command: {button}")
            log(LogLevel.INFO, f"Sending IR command: {button}…")
            success = ir_send_command(button)
            if not success:
                log(LogLevel.WARN, "IR command failed — check pi_server")
        else:
            log(LogLevel.WARN,
                f'Invalid command; continuing command mode. (heard: "{cleaned}")')

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    """Entry point"""
    print("\n" + "=" * 70)
    print(" 🎤 MAVEN Voice Assistant — Powered by Whisper")
    print("=" * 70 + "\n")

    # ── Token status report ───────────────────────────────────────────────────
    if IR_TOKEN:
        log(LogLevel.INFO, f"✅ IR token loaded ({IR_TOKEN[:6]}…)")
    else:
        log(LogLevel.WARN, "IR token not found. IR commands will not fire.")
        log(LogLevel.WARN,
            "Add /api/local/token to pi_server.py — see comment near top of this file.")
        print()

     # ── Startup Health Checks ─────────────────────────────────────

    log(LogLevel.INFO, "Running startup health checks...")

    try:
        r = requests.get(f"{MIC_SERVER_URL}/status", timeout=2)
        if r.status_code == 200 and r.json().get("ok"):
            log(LogLevel.INFO, "✅ Mic server OK")
        else:
            log(LogLevel.WARN, "⚠️ Mic server reachable but not healthy")
    except Exception as e:
        log(LogLevel.ERROR, f"❌ Mic server unreachable: {e}")

    try:
        r = requests.get(f"{IR_SERVER_URL}/api/local/token", timeout=2)
        if r.status_code == 200:
            log(LogLevel.INFO, "✅ IR server OK")
        else:
            log(LogLevel.WARN, f"⚠️ IR server HTTP {r.status_code}")
    except Exception as e:
        log(LogLevel.ERROR, f"❌ IR server unreachable: {e}")
    
    start_state_server()
    init_whisper()

    assistant = VoiceAssistant()
    assistant.run()

if __name__ == "__main__":
    main()