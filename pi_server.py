#!/usr/bin/env python3
"""
MAVEN Pi Server
Run: python3 pi_server.py
Hold GPIO 24 for 5 seconds to enter pairing/discovery mode.
"""

import pigpio
import time
import json
import sqlite3
import threading
import os
import secrets
import hashlib
from flask import Flask, jsonify, request, abort, render_template_string
from flask_cors import CORS

# ── configuration ─────────────────────────────────────────────────────────────
IR_RECV_PIN    = 27
IR_LED_PIN     = 17
STATUS_LED_PIN = 22   # red LED
LISTEN_LED_PIN = 23   # green LED
PAIR_BTN_PIN   = 24   # pairing button, positive logic

CARRIER_HZ     = 38_000
CARRIER_DUTY   = 0.33
PACKET_GAP     = 15_000
DB_FILE        = os.path.join(os.path.dirname(__file__), "ir_codes.db")

PAIRING_TIMEOUT  = 15   # seconds pairing mode stays active
LEARNING_TIMEOUT = 15   # seconds to wait for IR pulse

BUTTON_ORDER = [
    "power_toggle", "vol_up", "vol_down", "mute",
    "channel_up", "channel_down",
    "home", "left", "up", "right", "down", "enter", "return",
]

# ── session token (simple security) ──────────────────────────────────────────
# Generated fresh each run; app must include it in all requests after pairing.
SESSION_TOKEN = secrets.token_hex(16)

# ── database ──────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ir_codes (
            id     INTEGER PRIMARY KEY,
            name   TEXT    NOT NULL UNIQUE,
            pulses TEXT
        )""")
    conn.commit()
    for i, name in enumerate(BUTTON_ORDER, 1):
        conn.execute(
            "INSERT OR IGNORE INTO ir_codes (id, name, pulses) VALUES (?,?,NULL)",
            (i, name)
        )
    conn.commit()
    return conn

def db_save(conn, name, pulses):
    conn.execute("UPDATE ir_codes SET pulses=? WHERE name=?",
                 (json.dumps(pulses), name))
    conn.commit()

def db_clear_one(conn, name):
    conn.execute("UPDATE ir_codes SET pulses=NULL WHERE name=?", (name,))
    conn.commit()

def db_clear_all(conn):
    conn.execute("UPDATE ir_codes SET pulses=NULL")
    conn.commit()

def db_get_all(conn):
    return conn.execute(
        "SELECT id, name, pulses FROM ir_codes ORDER BY id"
    ).fetchall()

# ── IR receive ────────────────────────────────────────────────────────────────
class IRReceiver:
    def __init__(self, pi, pin):
        self.pi = pi
        self.pin = pin
        self._pulses = []
        self._last = None
        self._packet = None
        self._cb = pi.callback(pin, pigpio.EITHER_EDGE, self._edge)

    def _edge(self, gpio, level, tick):
        if self._last is None:
            self._last = tick
            return
        dur = pigpio.tickDiff(self._last, tick)
        self._last = tick
        if dur > PACKET_GAP:
            if self._pulses:
                self._packet = list(self._pulses)
                self._pulses = []
        else:
            self._pulses.append((level, dur))

    def wait_for_packet(self, timeout=15):
        self._packet = None
        self._pulses = []
        self._last = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._packet is not None:
                return self._packet
            time.sleep(0.05)
        return None

    def cancel(self):
        self._cb.cancel()

# ── IR transmit ───────────────────────────────────────────────────────────────
CARRIER_PERIOD_US = int(1_000_000 / CARRIER_HZ)
CARRIER_ON_US     = int(CARRIER_PERIOD_US * CARRIER_DUTY)
CARRIER_OFF_US    = CARRIER_PERIOD_US - CARRIER_ON_US

def _burst(us):
    out, elapsed = [], 0
    while elapsed < us:
        out.append(pigpio.pulse(1 << IR_LED_PIN, 0, CARRIER_ON_US))
        out.append(pigpio.pulse(0, 1 << IR_LED_PIN, CARRIER_OFF_US))
        elapsed += CARRIER_PERIOD_US
    return out

def _space(us):
    return [pigpio.pulse(0, 1 << IR_LED_PIN, us)]

def send_stored(pi_inst, stored_pulses):
    pulses = []
    for p in stored_pulses:
        pulses += _burst(p["duration"]) if p["level"] == 1 else _space(p["duration"])
    if not pulses:
        return
    pi_inst.wave_clear()
    pi_inst.wave_add_generic(pulses)
    wave = pi_inst.wave_create()
    if wave < 0:
        return
    pi_inst.wave_send_once(wave)
    while pi_inst.wave_tx_busy():
        time.sleep(0.001)
    pi_inst.wave_delete(wave)

# ── LED helpers ───────────────────────────────────────────────────────────────
class BlinkLED:
    def __init__(self, pi_inst, pin, on_ms=250, off_ms=250):
        self.pi = pi_inst
        self.pin = pin
        self.on_ms = on_ms
        self.off_ms = off_ms
        self._stop = threading.Event()
        self._t = None

    def start(self):
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join()
        self.pi.write(self.pin, 0)

    def _run(self):
        while not self._stop.is_set():
            self.pi.write(self.pin, 1)
            self._stop.wait(self.on_ms / 1000)
            self.pi.write(self.pin, 0)
            self._stop.wait(self.off_ms / 1000)

def flash_led(pi_inst, pin, duration=0.4):
    pi_inst.write(pin, 1)
    threading.Timer(duration, pi_inst.write, args=(pin, 0)).start()

# ── global state ──────────────────────────────────────────────────────────────
state = {
    "pairing":      False,
    "learning":     False,
    "learn_name":   None,
    "learn_result": None,
}
state_lock = threading.Lock()

pi          = None
conn        = None
receiver    = None
red_blink   = None
green_blink = None

# ── auth helper ───────────────────────────────────────────────────────────────
def require_token():
    """Call at the top of any protected endpoint."""
    token = request.headers.get("X-Maven-Token") or request.args.get("token")
    if token != SESSION_TOKEN:
        abort(403)

# ── pairing button watcher ────────────────────────────────────────────────────
def button_watcher():
    hold_start = None
    while True:
        level = pi.read(PAIR_BTN_PIN)
        if level == 1:
            if hold_start is None:
                hold_start = time.time()
            elif time.time() - hold_start >= 3.0:
                enter_pairing()
                hold_start = None
        else:
            hold_start = None
        time.sleep(0.05)

def enter_pairing():
    with state_lock:
        if state["pairing"]:
            return
        state["pairing"] = True
    red_blink.start()
    print("[MAVEN] Pairing mode active — discoverable for 15s")
    threading.Timer(PAIRING_TIMEOUT, exit_pairing).start()

def exit_pairing():
    with state_lock:
        state["pairing"] = False
    red_blink.stop()
    print("[MAVEN] Pairing mode ended.")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── DISCOVERY (no token required — but only responds when pairing) ────────────
@app.route("/api/discover")
def api_discover():
    """
    Scan target: app hits this endpoint to check if a MAVEN is present.
    Returns 200 with pairing=True only while the button has been held.
    Always returns ok=True so the scanner knows something is here,
    but pairing=False means 'found but not ready'.
    """
    with state_lock:
        pairing = state["pairing"]
    return jsonify({
        "ok":      True,
        "pairing": pairing,
        "name":    "MAVEN",
    })

# ── PAIRING (no token required) ───────────────────────────────────────────────
@app.route("/api/confirm-pair", methods=["POST"])
def api_confirm_pair():
    """
    Called by the app when user taps a discovered MAVEN card.
    Returns the session token so all future calls can be authenticated.
    Only succeeds while pairing mode is active.
    """
    with state_lock:
        pairing = state["pairing"]
    if not pairing:
        return jsonify({"ok": False, "error": "not in pairing mode"}), 403
    exit_pairing()
    return jsonify({"ok": True, "token": SESSION_TOKEN})

# ── PROTECTED ENDPOINTS ───────────────────────────────────────────────────────

@app.route("/api/local/token")
def api_local_token():
    if request.remote_addr != "127.0.0.1":
        abort(403)
    return jsonify({"ok": True, "token": SESSION_TOKEN})

@app.route("/api/codes")
def api_codes():
    require_token()
    rows = db_get_all(conn)
    codes = [
        {"id": r["id"], "name": r["name"], "learned": r["pulses"] is not None}
        for r in rows
    ]
    with state_lock:
        lname   = state["learn_name"]
        lresult = state["learn_result"]
    return jsonify({"codes": codes, "learning": lname, "learn_result": lresult})

@app.route("/api/learn/<name>", methods=["POST"])
def api_learn(name):
    require_token()
    if name not in BUTTON_ORDER:
        return jsonify({"ok": False, "error": "unknown button"}), 400
    with state_lock:
        if state["learning"]:
            return jsonify({"ok": False, "error": "already learning"}), 409
        state["learning"]   = True
        state["learn_name"] = name
        state["learn_result"] = None
    threading.Thread(target=_do_learn, args=(name,), daemon=True).start()
    return jsonify({"ok": True})

def _do_learn(name):
    green_blink.start()
    print(f"[MAVEN] Learning '{name}'...")
    packet = receiver.wait_for_packet(timeout=LEARNING_TIMEOUT)
    green_blink.stop()
    if packet:
        pulses = [{"level": lvl, "duration": dur} for lvl, dur in packet]
        db_save(conn, name, pulses)

        green_blink.stop()
        pi.write(LISTEN_LED_PIN, 1)
        threading.Timer(1.0, pi.write, args=(LISTEN_LED_PIN, 0)).start()

        #flash_led(pi, STATUS_LED_PIN, 0.3)
        result = "ok"
        print(f"[MAVEN] Saved '{name}'.")
    else:
        result = "timeout"
        print(f"[MAVEN] Timed out learning '{name}'.")
    with state_lock:
        state["learning"]     = False
        state["learn_name"]   = None
        state["learn_result"] = result
    threading.Timer(3, _clear_result).start()

def _clear_result():
    with state_lock:
        state["learn_result"] = None

@app.route("/api/send/<name>", methods=["POST"])
def api_send(name):
    require_token()
    if name not in BUTTON_ORDER:
        return jsonify({"ok": False, "error": "unknown button"}), 400
    row = conn.execute(
        "SELECT pulses FROM ir_codes WHERE name=?", (name,)
    ).fetchone()
    if not row or not row["pulses"]:
        return jsonify({"ok": False, "error": "not learned"}), 404
    stored = json.loads(row["pulses"])
    threading.Thread(
        target=send_stored, args=(pi, stored), daemon=True
    ).start()
    flash_led(pi, STATUS_LED_PIN, 0.3)
    return jsonify({"ok": True})

@app.route("/api/clear/<name>", methods=["POST"])
def api_clear_one(name):
    require_token()
    if name not in BUTTON_ORDER:
        return jsonify({"ok": False, "error": "unknown button"}), 400
    db_clear_one(conn, name)
    return jsonify({"ok": True})

@app.route("/api/clear-all", methods=["POST"])
def api_clear_all_route():
    require_token()
    db_clear_all(conn)
    return jsonify({"ok": True})


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        return render_template_string(f.read())

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    global pi, conn, receiver, red_blink, green_blink

    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod not running — sudo systemctl start pigpiod")

    pi.set_mode(IR_RECV_PIN,    pigpio.INPUT)
    pi.set_pull_up_down(IR_RECV_PIN, pigpio.PUD_UP)
    pi.set_mode(IR_LED_PIN,     pigpio.OUTPUT)
    pi.write(IR_LED_PIN, 0)
    pi.set_mode(STATUS_LED_PIN, pigpio.OUTPUT)
    pi.write(STATUS_LED_PIN, 0)
    pi.set_mode(LISTEN_LED_PIN, pigpio.OUTPUT)
    pi.write(LISTEN_LED_PIN, 0)
    pi.set_mode(PAIR_BTN_PIN,   pigpio.INPUT)
    pi.set_pull_up_down(PAIR_BTN_PIN, pigpio.PUD_DOWN)

    conn        = db_connect()
    receiver    = IRReceiver(pi, IR_RECV_PIN)
    red_blink   = BlinkLED(pi, STATUS_LED_PIN, on_ms=200, off_ms=200)
    green_blink = BlinkLED(pi, LISTEN_LED_PIN, on_ms=250, off_ms=250)

    threading.Thread(target=button_watcher, daemon=True).start()

    print("\n=== MAVEN Pi Server ===")
    print(f"Hold GPIO {PAIR_BTN_PIN} for 3s to enter pairing mode.")
    print(f"Session token: {SESSION_TOKEN}")
    print(f"API on http://0.0.0.0:5000\n")

    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        red_blink.stop()
        green_blink.stop()
        receiver.cancel()
        pi.write(IR_LED_PIN, 0)
        pi.write(STATUS_LED_PIN, 0)
        pi.write(LISTEN_LED_PIN, 0)
        pi.stop()
        conn.close()

if __name__ == "__main__":
    main()