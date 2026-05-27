"""
MAVEN Microphone Server
-----------------------
DVR-style rolling audio buffer for short voice commands.
Mirrors camera_server.py architecture.

Endpoints:
  /            -> web dashboard (hear latest audio, see volume)
  /status      -> mic health JSON
  /level       -> current RMS volume JSON
  /recent.wav  -> last N seconds as WAV file
  /recent.raw  -> last N seconds as raw PCM
  /trigger_capture -> freeze latest chunk, return WAV (for speech recognition)
"""

from flask import Flask, Response, jsonify
import pyaudio
import wave
import io
import time
import threading
import collections
import struct
import math

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

SAMPLE_RATE    = 48000
CHANNELS       = 1
SAMPLE_WIDTH   = 2          # 16-bit signed PCM = 2 bytes
CHUNK_SAMPLES  = 1024       # frames per capture chunk
BUFFER_SECONDS = 10         # rolling buffer length
ALSA_DEVICE    = "plughw:3,0"   # override: set None to auto-detect USB PnP

# Derived
CHUNKS_IN_BUFFER = int((SAMPLE_RATE / CHUNK_SAMPLES) * BUFFER_SECONDS)

# ─── Shared State ─────────────────────────────────────────────────────────────

BUFFER_LOCK    = threading.Lock()
audio_buffer   = collections.deque(maxlen=CHUNKS_IN_BUFFER)  # each element = bytes chunk
current_rms    = 0.0
mic_ok         = False
mic_error      = None

frozen_chunk   = None   # set by /trigger_capture
FROZEN_LOCK    = threading.Lock()


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Typical USB mic peaks at ~2000–4000 out of 32768 in normal speech.
# Using a realistic ceiling makes 5% real speech show as ~40%+ on the meter.
RMS_CEILING = 3000.0  # tune up if meter still clips; tune down if too sensitive

def compute_rms(raw_bytes):
    """Return RMS level 0.0–1.0 scaled to RMS_CEILING (not theoretical max)."""
    count = len(raw_bytes) // SAMPLE_WIDTH
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", raw_bytes)
    rms = math.sqrt(sum(s * s for s in shorts) / count)
    return min(rms / RMS_CEILING, 1.0)


def find_usb_pnp_device(p):
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)

        name = info.get("name", "").lower()

        print("DEVICE:", i, name)

        if (
            ("usb" in name or "pnp" in name or "audio" in name)
            and info["maxInputChannels"] > 0
        ):
            print("Using microphone:", name)
            return i

    return None


def buffer_to_wav(chunks):
    """Convert a list of PCM byte chunks into an in-memory WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        for chunk in chunks:
            wf.writeframes(chunk)
    buf.seek(0)
    return buf.read()


# ─── Mic Capture Thread ───────────────────────────────────────────────────────

def mic_loop():
    global mic_ok, mic_error, current_rms

    while True:  # outer restart loop — keeps recovering from hardware faults
        p = pyaudio.PyAudio()
        stream = None

        try:
            # Resolve device
            if ALSA_DEVICE:
                # Use PyAudio with ALSA string via device index lookup
                device_index = find_usb_pnp_device(p)
                if device_index is None:
                    raise OSError(f"USB PnP Sound Device not found (ALSA: {ALSA_DEVICE})")
            else:
                device_index = find_usb_pnp_device(p)
                if device_index is None:
                    raise OSError("USB PnP Sound Device not found (auto-detect failed)")

            stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=CHUNK_SAMPLES,
            )

            mic_ok = True
            mic_error = None

            while True:
                try:
                    data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
                    with BUFFER_LOCK:
                        audio_buffer.append(data)
                    current_rms = compute_rms(data)
                except OSError as e:
                    raise  # bubble up to outer restart

        except Exception as e:
            mic_ok = False
            mic_error = str(e)
            current_rms = 0.0

        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass

        # Wait before retrying (don't spin-burn CPU on repeated failures)
        time.sleep(5)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    with BUFFER_LOCK:
        buffered = len(audio_buffer)
    return jsonify({
        "ok":             mic_ok,
        "error":          mic_error,
        "sample_rate":    SAMPLE_RATE,
        "channels":       CHANNELS,
        "bit_depth":      SAMPLE_WIDTH * 8,
        "buffer_seconds": BUFFER_SECONDS,
        "chunks_stored":  buffered,
        "alsa_device":    ALSA_DEVICE,
    })


@app.route("/level")
def level():
    return jsonify({
        "rms":     round(current_rms, 4),
        "rms_pct": round(current_rms * 100, 1),
        "ok":      mic_ok,
    })


@app.route("/recent.raw")
def recent_raw():
    if not mic_ok:
        return "Microphone Not Found", 503
    with BUFFER_LOCK:
        chunks = list(audio_buffer)
    if not chunks:
        return "Buffer empty", 503
    raw = b"".join(chunks)
    return Response(raw, mimetype="application/octet-stream")


@app.route("/recent.wav")
def recent_wav():
    if not mic_ok:
        return "Microphone Not Found", 503
    with BUFFER_LOCK:
        chunks = list(audio_buffer)
    if not chunks:
        return "Buffer empty", 503
    wav_bytes = buffer_to_wav(chunks)
    return Response(wav_bytes, mimetype="audio/wav",
                    headers={"Content-Disposition": "inline; filename=recent.wav"})


@app.route("/trigger_capture")
def trigger_capture():
    """
    Freeze the current rolling buffer into a snapshot.
    Use this when a wake word / gesture / button fires.
    Returns the frozen chunk as WAV immediately.
    Downstream speech recognition should call this endpoint.
    """
    global frozen_chunk
    if not mic_ok:
        return "Microphone Not Found", 503
    with BUFFER_LOCK:
        chunks = list(audio_buffer)
    if not chunks:
        return "Buffer empty", 503
    wav_bytes = buffer_to_wav(chunks)
    with FROZEN_LOCK:
        frozen_chunk = wav_bytes
    return Response(wav_bytes, mimetype="audio/wav",
                    headers={"Content-Disposition": "attachment; filename=command.wav"})


@app.route("/")
def index():
    error_html = ""
    if not mic_ok:
        error_html = f'<div class="error-banner">⚠ {mic_error or "Microphone Not Found"}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MAVEN · Microphone</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@300;500;700&display=swap');

    :root {{
      --bg:       #0a0c0f;
      --panel:    #0f1318;
      --border:   #1e2530;
      --accent:   #00e5ff;
      --accent2:  #7c4dff;
      --warn:     #ff4b4b;
      --text:     #c8d6e5;
      --dim:      #4a5568;
      --green:    #00ff88;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Rajdhani', sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2rem 1rem;
      background-image:
        radial-gradient(ellipse at 20% 0%, rgba(0,229,255,0.04) 0%, transparent 60%),
        radial-gradient(ellipse at 80% 100%, rgba(124,77,255,0.04) 0%, transparent 60%);
    }}

    header {{
      text-align: center;
      margin-bottom: 2.5rem;
    }}

    .logo {{
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.75rem;
      letter-spacing: 0.4em;
      color: var(--accent);
      text-transform: uppercase;
      margin-bottom: 0.4rem;
    }}

    h1 {{
      font-size: 2.8rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}

    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      width: 100%;
      max-width: 720px;
    }}

    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.4rem;
    }}

    .card.wide {{ grid-column: 1 / -1; }}

    .card-label {{
      font-family: 'Share Tech Mono', monospace;
      font-size: 0.65rem;
      letter-spacing: 0.3em;
      color: var(--dim);
      text-transform: uppercase;
      margin-bottom: 0.8rem;
    }}

    /* Status dot */
    .status-row {{
      display: flex;
      align-items: center;
      gap: 0.7rem;
      font-size: 1.1rem;
      font-weight: 500;
    }}

    .dot {{
      width: 10px; height: 10px;
      border-radius: 50%;
      background: var(--warn);
      flex-shrink: 0;
    }}
    .dot.ok {{ background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: 0.4; }}
    }}

    /* Volume meter */
    .meter-wrap {{
      height: 12px;
      background: var(--border);
      border-radius: 6px;
      overflow: hidden;
      margin-top: 0.5rem;
    }}

    .meter-bar {{
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--green), var(--accent));
      border-radius: 6px;
      transition: width 0.1s ease;
    }}

    .meter-pct {{
      font-family: 'Share Tech Mono', monospace;
      font-size: 1.6rem;
      color: var(--accent);
      margin-top: 0.3rem;
    }}

    /* Waveform canvas */
    canvas {{
      width: 100%;
      height: 60px;
      display: block;
      border-radius: 6px;
      background: #080a0d;
    }}

    /* Audio player */
    audio {{
      width: 100%;
      margin-top: 0.6rem;
      filter: invert(1) hue-rotate(180deg);
      border-radius: 6px;
    }}

    /* Buttons */
    .btn {{
      display: inline-block;
      padding: 0.6rem 1.4rem;
      border: 1px solid var(--accent);
      color: var(--accent);
      background: transparent;
      border-radius: 6px;
      font-family: 'Rajdhani', sans-serif;
      font-weight: 600;
      font-size: 0.95rem;
      letter-spacing: 0.08em;
      cursor: pointer;
      transition: background 0.2s, color 0.2s;
      text-transform: uppercase;
    }}

    .btn:hover {{ background: var(--accent); color: var(--bg); }}

    .btn.trigger {{
      border-color: var(--accent2);
      color: var(--accent2);
    }}
    .btn.trigger:hover {{ background: var(--accent2); color: #fff; }}

    .btn-row {{
      display: flex;
      gap: 0.8rem;
      flex-wrap: wrap;
      margin-top: 0.8rem;
    }}

    .error-banner {{
      background: rgba(255,75,75,0.12);
      border: 1px solid var(--warn);
      color: var(--warn);
      padding: 0.7rem 1.2rem;
      border-radius: 8px;
      margin-bottom: 1.5rem;
      font-weight: 500;
      width: 100%;
      max-width: 720px;
      text-align: center;
    }}

    .mono {{ font-family: 'Share Tech Mono', monospace; font-size: 0.85rem; color: var(--dim); }}

    #trigger-result {{
      margin-top: 0.5rem;
      font-size: 0.85rem;
      color: var(--accent2);
      font-family: 'Share Tech Mono', monospace;
      min-height: 1.2em;
    }}
  </style>
</head>
<body>
  <header>
    <div class="logo">MAVEN · Audio Module</div>
    <h1>MICROPHONE</h1>
  </header>

  {error_html}

  <div class="grid">

    <!-- Status -->
    <div class="card">
      <div class="card-label">System Status</div>
      <div class="status-row">
        <div class="dot" id="status-dot"></div>
        <span id="status-text">Connecting…</span>
      </div>
      <div class="mono" id="status-detail" style="margin-top:0.5rem"></div>
    </div>

    <!-- Volume -->
    <div class="card">
      <div class="card-label">Input Level</div>
      <div class="meter-pct" id="rms-pct">0.0%</div>
      <div class="meter-wrap"><div class="meter-bar" id="meter-bar"></div></div>
    </div>

    <!-- Waveform -->
    <div class="card wide">
      <div class="card-label">Live Waveform (RMS history)</div>
      <canvas id="wave-canvas"></canvas>
    </div>

    <!-- Audio playback -->
    <div class="card wide">
      <div class="card-label">Recent Audio (last {BUFFER_SECONDS}s)</div>
      <audio id="audio-player" controls></audio>
      <div class="btn-row">
        <button class="btn" onclick="loadAudio()">↺ Load Latest Audio</button>
        <button class="btn trigger" onclick="triggerCapture()">⚡ Trigger Capture</button>
      </div>
      <div id="trigger-result"></div>
    </div>

  </div>

  <script>
    const rmsHistory = new Array(120).fill(0);
    const canvas = document.getElementById('wave-canvas');
    const ctx = canvas.getContext('2d');

    function drawWave() {{
      const W = canvas.offsetWidth, H = 60;
      canvas.width = W; canvas.height = H;
      ctx.clearRect(0, 0, W, H);
      const barW = W / rmsHistory.length;
      rmsHistory.forEach((v, i) => {{
        // Boost visual height: square-root stretches low values upward
        const boosted = Math.pow(v, 0.5);
        const h = Math.max(2, boosted * H * 0.95);
        const alpha = 0.25 + boosted * 0.75;
        ctx.fillStyle = `rgba(0, 229, 255, ${{alpha}})`;
        ctx.fillRect(i * barW, (H - h) / 2, Math.max(1, barW - 1), h);
      }});
    }}

    async function pollLevel() {{
      try {{
        const r = await fetch('/level');
        const d = await r.json();
        const pct = d.rms_pct.toFixed(1);
        document.getElementById('rms-pct').textContent = pct + '%';
        document.getElementById('meter-bar').style.width = Math.min(pct, 100) + '%';
        rmsHistory.shift();
        rmsHistory.push(d.rms);
        drawWave();
      }} catch(e) {{}}
      setTimeout(pollLevel, 100);
    }}

    async function pollStatus() {{
      try {{
        const r = await fetch('/status');
        const d = await r.json();
        const dot = document.getElementById('status-dot');
        const txt = document.getElementById('status-text');
        const det = document.getElementById('status-detail');
        if (d.ok) {{
          dot.className = 'dot ok';
          txt.textContent = 'Microphone Active';
          det.textContent = `${{d.sample_rate}} Hz · ${{d.bit_depth}}-bit · mono · ${{d.chunks_stored}} chunks buffered`;
        }} else {{
          dot.className = 'dot';
          txt.textContent = 'Microphone Not Found';
          det.textContent = d.error || '';
        }}
      }} catch(e) {{}}
      setTimeout(pollStatus, 3000);
    }}

    function loadAudio() {{
      const player = document.getElementById('audio-player');
      player.src = '/recent.wav?t=' + Date.now();
      player.load();
    }}

    async function triggerCapture() {{
      const el = document.getElementById('trigger-result');
      el.textContent = 'Capturing…';
      try {{
        const r = await fetch('/trigger_capture');
        if (r.ok) {{
          el.textContent = '✓ Chunk frozen — ' + new Date().toLocaleTimeString();
          loadAudio();
        }} else {{
          el.textContent = '✗ ' + r.statusText;
        }}
      }} catch(e) {{
        el.textContent = '✗ ' + e.message;
      }}
    }}

    pollLevel();
    pollStatus();
    drawWave();
  </script>
</body>
</html>
"""


# ─── Boot ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=mic_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8082, threaded=True)