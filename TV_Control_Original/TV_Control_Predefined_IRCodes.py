#!/usr/bin/env python3
import pigpio
import time

# configuring 38kHZ which is common for Samsung TVs
IR_LED_PIN   = 17
CARRIER_HZ   = 38000 # the IR LED is already modulated at 38kHZ
CARRIER_DUTY = 0.33

# pigpio initialization
pi = pigpio.pi()
if not pi.connected:
    raise RuntimeError("pigpio daemon not running")

pi.set_mode(IR_LED_PIN, pigpio.OUTPUT)
pi.write(IR_LED_PIN, 0)

CARRIER_PERIOD_US = int(1_000_000 / CARRIER_HZ) # ~26 us
CARRIER_ON_US     = int(CARRIER_PERIOD_US * CARRIER_DUTY) # ~9 us
CARRIER_OFF_US    = CARRIER_PERIOD_US - CARRIER_ON_US # ~17 us

# bursts and spaces used to construct bits... 1's are 560us burst + 1690us space, 0's are 560us burst + 560us space
def _burst(duration_us):
    pulses = []
    elapsed = 0
    while elapsed < duration_us:
        pulses.append(pigpio.pulse(1 << IR_LED_PIN, 0, CARRIER_ON_US))
        pulses.append(pigpio.pulse(0, 1 << IR_LED_PIN, CARRIER_OFF_US))
        elapsed += CARRIER_PERIOD_US
    return pulses

def _space(duration_us):
    return [pigpio.pulse(0, 1 << IR_LED_PIN, duration_us)]

# bits are sent LSB first, so we shift right and mask with 0x01 to get each bit
def _send_bit(bit):
    pulses = _burst(560) # 560us burst for both 1 and 0
    pulses += _space(1690) if bit else _space(560) # 1690us space for 1, 560us space for 0
    return pulses

def _send_byte(data):
    pulses = []
    for i in range(8):
        pulses += _send_bit((data >> i) & 0x01)  # LSB first
    return pulses

def _send_wave(pulses):
    pi.wave_clear()
    pi.wave_add_generic(pulses)
    wave = pi.wave_create()
    if wave < 0:
        print("Wave creation failed")
        return
    pi.wave_send_once(wave)
    while pi.wave_tx_busy():
        time.sleep(0.001)
    pi.wave_delete(wave)

def send_packet(b0, b1, b2, b3):
    pulses  = _burst(9000) # 9ms leader burst (header)
    pulses += _space(4500) # 4.5ms leader space (header)
    pulses += _send_byte(b0) # address
    pulses += _send_byte(b1) # ~address
    pulses += _send_byte(b2) # command
    pulses += _send_byte(b3) # ~command
    pulses += _burst(560) # stop bit (end of packet)
    pulses += _space(560)
    _send_wave(pulses)

# Samsung control commands (all parameters are 1 byte long, sent LSB first)
def power_toggle():  send_packet(0x07, 0x07, 0x02, 0xFD)
# def power_toggle():  send_packet(0x07, 0x07, 0xE6, 0x19) # new captured code from TV_Control_Learn_IRCodes.py
def cursor_up():     send_packet(0x07, 0x07, 0x60, 0x9F)
def cursor_down():   send_packet(0x07, 0x07, 0x61, 0x9E)
def cursor_enter():  send_packet(0x07, 0x07, 0x68, 0x97)
def cursor_left():   send_packet(0x07, 0x07, 0x65, 0x9A)
def cursor_right():  send_packet(0x07, 0x07, 0x62, 0x9D)

# command map
COMMANDS = {
    'p': ("Power Toggle",  power_toggle),
    'w': ("Cursor Up",     cursor_up),
    's': ("Cursor Down",   cursor_down),
    'a': ("Cursor Left",   cursor_left),
    'd': ("Cursor Right",  cursor_right),
    'e': ("Cursor Enter",  cursor_enter),
}

# main loop
if __name__ == "__main__":
    print("\n=== Samsung TV IR Control ===")
    print("p = Power Toggle")
    print("w = Cursor Up")
    print("s = Cursor Down")
    print("e = Cursor Enter")
    print("a = Cursor Left")
    print("d = Cursor Right")
    print("q = Quit\n")
    print("Point IR LED at TV and press a key + Enter\n")

    try:
        while True:
            key = input("Key: ").strip().lower()
            if key == 'q':
                break
            if key in COMMANDS:
                label, fn = COMMANDS[key]
                print(f"  Sending {label}...", end=" ", flush=True)
                fn()
                print("done")
            else:
                print(f"  Unknown key — use: {', '.join(COMMANDS.keys())}, q")

    except KeyboardInterrupt:
        pass

    finally:
        pi.write(IR_LED_PIN, 0)
        pi.stop()
        print("\nDone.")