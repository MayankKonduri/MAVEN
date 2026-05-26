#!/usr/bin/env python3
"""
learn_ir_code.py — IR Code Learner
===================================
Learns 13 standard TV buttons in order and saves them to ir_codes.db (SQLite).

Button order:
    1  power_toggle    2  vol_up        3  vol_down
    4  mute            5  channel_up    6  channel_down
    7  home            8  left          9  up
    10 right           11 down          12 enter
    13 return

Usage:
    python3 learn_ir_code.py

Commands:
    l = learn   steps through all empty slots in order; timeout to skip
    v = view    shows all 13 buttons with decoded bytes or Empty
    c = clear all

Wiring:
    TSOP32438 pin 1 (OUT) → GPIO 27 (BCM) / physical pin 13
    TSOP32438 pin 2 (GND) → any GND
    TSOP32438 pin 3 (VS)  → 3.3V
"""

import pigpio
import time
import json
import sqlite3

# ── Config ────────────────────────────────────────────────────────────────────

IR_RECV_PIN = 27
PACKET_GAP  = 15_000
DB_FILE     = "ir_codes.db"

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

# ── Database ──────────────────────────────────────────────────────────────────

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

# ── IR receive ────────────────────────────────────────────────────────────────

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

# ── Decode helpers ────────────────────────────────────────────────────────────

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

# ── Learn flow ────────────────────────────────────────────────────────────────

def learn_one_code(receiver, conn, name):
    """
    Learn a single named button.
    Returns 'saved', 'skip', or 'quit'.
    """
    print(f"\n  [{name}]  Point remote at sensor and press the button...")
    ans = input("  Ready to capture? (y/n): ").strip().lower()
    if ans != 'y':
        return "skip"

    # Flush any stale packet sitting in the buffer from previous activity
    receiver._packet = None
    receiver._pulses = []

    # Flush buffer — discard anything captured before the prompt
    receiver._packet = None
    receiver._pulses = []
    receiver._last   = None

    packet = receiver.wait_for_packet(timeout=15)
    if packet is None:
        print("  Timed out — skipping.")
        return "skip"

    # The packet was just finalized — wait briefly for the line to go idle
    # so the next button press doesn't bleed into this capture
    time.sleep(0.5)

    print_packet(packet)
    db_save(conn, name, pulses_to_storable(packet))
    print(f"  ✓ Saved '{name}'.")
    return "saved"

def learn_all(receiver, conn):
    empty = [name for name in BUTTON_ORDER if not db_is_learned(conn, name)]
    if not empty:
        print("\n  All 13 buttons already learned. Use 'c' to clear and re-learn.")
        return

    total   = len(empty)
    learned = 0
    print(f"\n  {total} button(s) remaining: {', '.join(empty)}")
    print( "  Tip: wait out the 15s timeout to skip a button.\n")

    for name in empty:
        result = learn_one_code(receiver, conn, name)
        if result == "saved":
            learned += 1
        elif result == "quit":
            learned += 1
            break

    print(f"\n  Done — learned {learned}/{total} buttons this session.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod not running — sudo systemctl start pigpiod")

    pi.set_mode(IR_RECV_PIN, pigpio.INPUT)
    pi.set_pull_up_down(IR_RECV_PIN, pigpio.PUD_UP)

    receiver = IRReceiver(pi, IR_RECV_PIN)
    conn     = db_connect()

    print("\n=== IR Code Learner ===")
    print(f"Database: {DB_FILE}")
    print("Commands:  l = learn   v = view   c = clear all")

    try:
        while True:
            cmd = input("\n> ").strip().lower()
            if   cmd == 'l': learn_all(receiver, conn)
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
        receiver.cancel()
        conn.close()
        pi.stop()
        print("\nDone.")

if __name__ == "__main__":
    main()