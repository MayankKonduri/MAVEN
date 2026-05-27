#!/usr/bin/env python3
import pigpio
import time
import json
import sqlite3
import threading

# configuration

IR_RECV_PIN    = 27
PACKET_GAP     = 15_000
DB_FILE        = "ir_codes.db"
LISTEN_LED_PIN = 23

BUTTON_ORDER = [
    "power_toggle",
    "vol_up",
    "vol_down",
    "mute",
    "channel_up",
    "channel_down",
    "home",
    "left",
    "up",
    "right",
    "down",
    "enter",
    "return",
]

# SQL Database helper function for storing learned codes.

def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ir_codes (
            id     INTEGER PRIMARY KEY,   -- fixed: 1-13 matching BUTTON_ORDER
            name   TEXT    NOT NULL UNIQUE,
            pulses TEXT                   -- NULL = empty slot
        )
    """)
    conn.commit()
    # Pre-populate all 13 rows with NULL pulses if not already present
    for i, name in enumerate(BUTTON_ORDER, 1):
        conn.execute("INSERT OR IGNORE INTO ir_codes (id, name, pulses) VALUES (?, ?, NULL)",
                     (i, name))
    conn.commit()
    return conn

def db_save(conn, name, pulses):
    conn.execute("UPDATE ir_codes SET pulses = ? WHERE name = ?",
                 (json.dumps(pulses), name))
    conn.commit()

def db_is_learned(conn, name):
    row = conn.execute("SELECT pulses FROM ir_codes WHERE name = ?", (name,)).fetchone()
    return row is not None and row["pulses"] is not None

def db_get_pulses(conn, name):
    row = conn.execute("SELECT pulses FROM ir_codes WHERE name = ?", (name,)).fetchone()
    if row and row["pulses"]:
        return json.loads(row["pulses"])
    return None

def db_view(conn):
    print(f"\n  {'#':<4} {'Button':<16} {'Status'}")
    print(f"  {'-'*60}")
    for row in conn.execute("SELECT id, name, pulses FROM ir_codes ORDER BY id"):
        if row["pulses"] is None:
            status = "Empty"
        else:
            pulses  = json.loads(row["pulses"])
            decoded = decode_nec([(p["level"], p["duration"]) for p in pulses])
            if decoded:
                b0, b1, b2, b3 = decoded
                status = f"addr=0x{b0:02X}  cmd=0x{b2:02X}  (~cmd=0x{b3:02X})"
            else:
                status = "Saved (non-NEC)"
        print(f"  {row['id']:<4} {row['name']:<16} {status}")
    print()

# IR receive logic

class IRReceiver:
    def __init__(self, pi, pin):
        self.pi      = pi
        self.pin     = pin
        self._pulses = []
        self._last   = None
        self._packet = None
        self._cb     = pi.callback(pin, pigpio.EITHER_EDGE, self._edge)

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
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._packet is not None:
                return self._packet
            time.sleep(0.05)
        return None

    def cancel(self):
        self._cb.cancel()

# helper functions to convert between raw pulses and pigpio waveforms, and to decode NEC protocol from raw pulses

def decode_nec(pulses):
    """
    Try to decode NEC/Samsung protocol from raw pulses.
    Returns (b0, b1, b2, b3) as ints, or None if not clean NEC.

    Pulse format from sensor (active-LOW output):
      level=0 → sensor LOW  → IR burst
      level=1 → sensor HIGH → IR space

    NEC: 9ms burst + 4.5ms space leader, then 32 bits LSB first.
    Bit = 560us burst + (560us space = 0, 1690us space = 1)
    """
    if len(pulses) < 4:
        return None
    i    = 2   # skip leader burst and leader space
    bits = []
    while i + 1 < len(pulses) and len(bits) < 32:
        _, space_dur = pulses[i + 1]
        if   400  < space_dur < 900:  bits.append(0)
        elif 1400 < space_dur < 2000: bits.append(1)
        else: return None
        i += 2
    if len(bits) < 32:
        return None
    def to_byte(b):
        return sum(bit << j for j, bit in enumerate(b))
    return (to_byte(bits[0:8]), to_byte(bits[8:16]),
            to_byte(bits[16:24]), to_byte(bits[24:32]))

def print_packet(pulses):
    print(f"\n  Total pulses: {len(pulses)}")
    print(f"  {'#':<4} {'Level':<8} {'Duration (us)':<16} {'Note'}")
    print(f"  {'-'*52}")
    for i, (level, dur) in enumerate(pulses):
        lvl  = "HIGH" if level else "LOW "
        note = ""
        if i == 0:   note = "leader burst (inverted)" if dur > 4000 else ""
        elif i == 1: note = "leader space"            if dur > 4000 else ""
        elif i % 2 == 0: note = "bit burst"
        else:
            if   400  < dur < 900:  note = "bit space → 0"
            elif 1400 < dur < 2000: note = "bit space → 1"
        print(f"  {i:<4} {lvl:<8} {dur:<16} {note}")
    decoded = decode_nec(pulses)
    if decoded:
        b0, b1, b2, b3 = decoded
        print(f"\n  Decoded NEC bytes:")
        print(f"    Address:  0x{b0:02X}  (~address: 0x{b1:02X})")
        print(f"    Command:  0x{b2:02X}  (~command: 0x{b3:02X})")
        if (b2 ^ b3) != 0xFF:
            print(f"    ⚠ Command check failed — Samsung extended NEC (expected)")
    else:
        print("\n  Could not decode as NEC — raw pulses saved correctly.")

def pulses_to_storable(pulses):
    return [{"level": lvl, "duration": dur} for lvl, dur in pulses]

# listen LED blink logic

class ListenLED:
    def __init__(self, pi, pin):
        self.pi       = pi
        self.pin      = pin
        self._stop    = threading.Event()
        self._thread  = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._blink, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.pi.write(self.pin, 0)

    def _blink(self):
        while not self._stop.is_set():
            self.pi.write(self.pin, 1)
            self._stop.wait(0.25)
            self.pi.write(self.pin, 0)
            self._stop.wait(0.25)

# learning flow for user interaction to capture and store IR codes in the database, one button at a time

def learn_one_code(receiver, conn, name, listen_led):
    """
    Learn a single named button.
    Returns 'saved', 'skip', or 'quit'.
    """
    print(f"\n  [{name}]  Point remote at sensor and press the button...")
    ans = input("  Ready to capture? (y/n): ").strip().lower()
    if ans != 'y':
        return "skip"

    # resetting state before capture to avoid bleed-over from previous captures
    receiver._packet = None
    receiver._pulses = []
    receiver._packet = None
    receiver._pulses = []
    receiver._last   = None

    listen_led.start()
    packet = receiver.wait_for_packet(timeout=15)
    listen_led.stop()

    if packet is None:
        print("  Timed out — skipping.")
        return "skip"

    time.sleep(0.5)

    print_packet(packet)
    db_save(conn, name, pulses_to_storable(packet))
    print(f"  ✓ Saved '{name}'.")
    return "saved"

def learn_all(receiver, conn, listen_led):
    empty = [name for name in BUTTON_ORDER if not db_is_learned(conn, name)]
    if not empty:
        print("\n  All 13 buttons already learned. Use 'c' to clear and re-learn.")
        return

    total   = len(empty)
    learned = 0
    print(f"\n  {total} button(s) remaining: {', '.join(empty)}")
    print( "  Tip: wait out the 15s timeout to skip a button.\n")

    for name in empty:
        result = learn_one_code(receiver, conn, name, listen_led)
        if result == "saved":
            learned += 1
        elif result == "quit":
            learned += 1
            break

    print(f"\n  Done — learned {learned}/{total} buttons this session.")

# Main loop 

def main():
    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod not running — sudo systemctl start pigpiod")

    pi.set_mode(IR_RECV_PIN, pigpio.INPUT)
    pi.set_pull_up_down(IR_RECV_PIN, pigpio.PUD_UP)

    pi.set_mode(LISTEN_LED_PIN, pigpio.OUTPUT)
    pi.write(LISTEN_LED_PIN, 0)

    receiver   = IRReceiver(pi, IR_RECV_PIN)
    conn       = db_connect()
    listen_led = ListenLED(pi, LISTEN_LED_PIN)

    print("\n=== IR Code Learner ===")
    print(f"Database: {DB_FILE}")
    print("Commands:  l = learn   v = view   c = clear all")

    try:
        while True:
            cmd = input("\n> ").strip().lower()
            if   cmd == 'l': learn_all(receiver, conn, listen_led)
            elif cmd == 'v': db_view(conn)
            elif cmd == 'c':
                if input("  Delete ALL codes? (y/n): ").strip().lower() == 'y':
                    conn.execute("UPDATE ir_codes SET pulses = NULL")
                    conn.commit()
                    print("  ✓ All codes cleared.")
            else:
                print("  Use l, v, or c.")
    except KeyboardInterrupt:
        pass
    finally:
        listen_led.stop()
        receiver.cancel()
        conn.close()
        pi.stop()
        print("\nDone.")

if __name__ == "__main__":
    main()