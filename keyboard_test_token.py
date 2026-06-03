#!/usr/bin/env python3

import requests

IR_SERVER_URL = "http://localhost:5000"


def load_ir_token():
    try:
        r = requests.get(
            f"{IR_SERVER_URL}/api/local/token",
            timeout=3
        )

        if r.status_code == 200:
            return r.json().get("token", "")

    except Exception as e:
        print("Failed to get token:", e)

    return ""


IR_TOKEN = load_ir_token()

if not IR_TOKEN:
    print("❌ No token found")
    exit(1)

print(f"✅ Token loaded: {IR_TOKEN[:8]}...")
print()


def send_command(button_name):

    headers = {
        "X-Maven-Token": IR_TOKEN
    }

    try:
        r = requests.post(
            f"{IR_SERVER_URL}/api/send/{button_name}",
            headers=headers,
            timeout=3
        )

        print("HTTP:", r.status_code)

        try:
            print(r.json())
        except:
            print(r.text)

    except Exception as e:
        print("Error:", e)


while True:

    cmd = input(
        "\nCommand "
        "(power_toggle, vol_up, vol_down, mute, "
        "channel_up, channel_down, home, left, up, "
        "right, down, enter, return, q): "
    ).strip()

    if cmd.lower() == "q":
        break

    send_command(cmd)