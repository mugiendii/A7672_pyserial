"""Send dummy sensor data from the A7672 until stopped (Ctrl+C), to see it arrive on the relay.

Uses the login stored on the modem by provision_device.py (C:/mqtt.json): no password needed here.
    .venv/bin/python dummy_data.py                 # every 10 s
    .venv/bin/python dummy_data.py --every 30

Publishes <id>/up/data, e.g. {"seq":1,"temp_c":23.4,"humidity":61.2,"battery_v":3.98,"ts":"..."},
and "online" on <id>/up/status. Watch it on the relay side:
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py
or in Grafana -> MQTT (https://monitor.nodewave.dev), "Message log".
"""
import argparse
import datetime
import json
import math
import random
import sys
import time

import serial

import mqtt_tls_test as dev
from setup import reset_mqtt


def dummy_reading(seq):
    """Plausible values that drift slowly, so a chart of them looks like a sensor."""
    t = time.time() / 600
    return {
        "seq": seq,
        "temp_c": round(23 + 3 * math.sin(t) + random.uniform(-0.2, 0.2), 1),
        "humidity": round(60 + 8 * math.cos(t) + random.uniform(-0.5, 0.5), 1),
        "battery_v": round(4.1 - (seq % 500) * 0.0005, 3),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def connect(ser):
    """Network, clock, TLS, login, status online. Returns True when ready to publish."""
    if dev.bring_up_network(ser) is None or not dev.check_clock(ser) or not dev.configure_ssl(ser):
        return False
    if not dev.connect_with_backoff(ser):
        return False
    dev.publish(ser, dev.TOPIC_STATUS, "online", qos=1)
    return True


def main():
    parser = argparse.ArgumentParser(description="Send dummy sensor data over MQTT from the A7672.")
    parser.add_argument("--every", type=int, default=10, help="seconds between readings (default 10)")
    args = parser.parse_args()

    ser = serial.Serial(dev.PORT, dev.BAUD, timeout=1)
    cfg = dev.read_device_config(ser)
    if not cfg:
        print(f"Modem has no login ({dev.DEVICE_CONFIG_PATH}). Run provision_device.py <id> first.")
        ser.close()
        sys.exit(1)
    dev.set_device(cfg)
    topic = f"{dev.DEVICE_ID}/up/data"
    print(f"Device {dev.DEVICE_ID}: sending to {topic} every {args.every} s (Ctrl+C to stop)")

    seq = 0
    connected = False
    try:
        while True:
            if not connected:
                connected = connect(ser)
                if not connected:
                    print("Not connected; retrying in 60 s")
                    time.sleep(60)
                    continue
            seq += 1
            if not dev.publish(ser, topic, json.dumps(dummy_reading(seq), separators=(",", ":")), qos=1):
                print("Publish failed: reconnecting")
                connected = False
                continue
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        if connected:
            dev.publish(ser, dev.TOPIC_STATUS, "offline", qos=1)
        reset_mqtt(ser)
        ser.close()
        print(f"Sent {seq} readings")


if __name__ == "__main__":
    main()
