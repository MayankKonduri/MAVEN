#!/usr/bin/env python3
import pigpio
import time
import json
import sqlite3
import threading

# configuration

IR_LED_PIN   = 17
CARRIER_HZ   = 38000
CARRIER_DUTY = 0.33
DB_FILE      = "ir_codes.db"

STATUS_LED_PIN = 22

# helper function to load learned codes from the database, returning a dict of {str(id): (name, pulses)} for only learned codes (pulses not NULL)

def load_learned_codes(db_file):
    """
    Load only learned (non-NULL) codes from the db.
    Returns dict of {str(id): (name, pulses)} preserving fixed IDs.
    """
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, pulses FROM ir_codes WHERE pulses IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.close()
    return {str(r["id"]): (r["name"], json.loads(r["pulses"])) for r in rows}

# IR transmission logic

CARRIER_PERIOD_US = int(1_000_000 / CARRIER_HZ)
CARRIER_ON_US     = int(CARRIER_PERIOD_US * CARRIER_DUTY)
CARRIER_OFF_US    = CARRIER_PERIOD_US - CARRIER_ON_US

def _burst(duration_us):
    pulses  = []
    elapsed = 0
    while elapsed < duration_us:
        pulses.append(pigpio.pulse(1 << IR_LED_PIN, 0, CARRIER_ON_US))
        pulses.append(pigpio.pulse(0, 1 << IR_LED_PIN, CARRIER_OFF_US))
        elapsed += CARRIER_PERIOD_US
    return pulses

def _space(duration_us):
    return [pigpio.pulse(0, 1 << IR_LED_PIN, duration_us)]

def _pulses_from_db(stored):
    """
    Reconstruct pigpio pulse list from stored db format.
    level=1 → pin just went HIGH → period before was LOW → IR burst
    level=0 → pin just went LOW  → period before was HIGH → silence
    """
    result = []
    for p in stored:
        if p["level"] == 1:
            result += _burst(p["duration"])
        else:
            result += _space(p["duration"])
    return result

def send_stored(pi, stored_pulses):
    pulses = _pulses_from_db(stored_pulses)
    if not pulses:
        print("  No pulses to send.")
        return
    pi.wave_clear()
    pi.wave_add_generic(pulses)
    wave = pi.wave_create()
    if wave < 0:
        print("  Wave creation failed.")
        return
    pi.wave_send_once(wave)
    while pi.wave_tx_busy():
        time.sleep(0.001)
    pi.wave_delete(wave)

# red led blink to indicate code was sent successfully
def flash_status_led(pi, duration=0.5):
    pi.write(STATUS_LED_PIN, 1)
    threading.Timer(duration, pi.write, args=(STATUS_LED_PIN, 0)).start()

# main loop

def main():
    shortcuts = load_learned_codes(DB_FILE)
    if not shortcuts:
        print(f"No learned codes found in {DB_FILE}.")
        print("Run learn_ir_code.py first.")
        return

    print("\n=== IR Remote ===")
    for key, (name, _) in sorted(shortcuts.items(), key=lambda x: int(x[0])):
        print(f"  {key} = {name}")
    print("  q = quit\n")

    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod not running — sudo systemctl start pigpiod")

    pi.set_mode(IR_LED_PIN, pigpio.OUTPUT)
    pi.write(IR_LED_PIN, 0)

    pi.set_mode(STATUS_LED_PIN, pigpio.OUTPUT)
    pi.write(STATUS_LED_PIN, 0)

    try:
        while True:
            key = input("Key: ").strip().lower()
            if key == 'q':
                break
            if key in shortcuts:
                name, pulses = shortcuts[key]
                print(f"  Sending {name}...", end=" ", flush=True)
                send_stored(pi, pulses)
                flash_status_led(pi)
                print("done")
            else:
                valid = sorted(shortcuts.keys(), key=int)
                print(f"  Unknown key — use: {', '.join(valid)}, q")
    except KeyboardInterrupt:
        pass
    finally:
        pi.write(IR_LED_PIN, 0)
        pi.write(STATUS_LED_PIN, 0)
        pi.stop()
        print("\nDone.")

if __name__ == "__main__":
    main()