"""Install a device on the platform: store its MQTT login on the modem once, on the bench.

The device keeps its login in the modem's flash (C:/mqtt.json) next to the CA certificate, so nobody
needs the password again after this. Run once per device, with the modem on /dev/ttyUSB0:
    .venv/bin/python provision_device.py oceo2

The password comes from Infisical (Nodewave / prod / /mqtt, key MQTT_DEVICE_<ID>_PASSWORD) through the
Infisical CLI, logged in once on this bench machine (e.g. a machine identity: export INFISICAL_TOKEN=...).
Or set MQTT_DEVICE_<ID>_PASSWORD in the environment. The login itself is created by the server team
(./add-device.sh <id>) - ask them for a batch of ids ahead of time.
"""
import argparse
import csv
import datetime
import json
import os
import re
import subprocess
import sys

import serial

import mqtt_tls_test as dev
from setup import send_at, reset_mqtt

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.csv")
INFISICAL_ENV = "prod"
INFISICAL_PATH = "/mqtt"


def password_key(device_id):
    return f"MQTT_DEVICE_{device_id.upper().replace('-', '_')}_PASSWORD"


def get_password(device_id):
    """Password from the environment, else from Infisical. Never printed."""
    key = password_key(device_id)
    if os.environ.get(key):
        return os.environ[key]
    cmd = ["infisical", "secrets", "get", key, f"--env={INFISICAL_ENV}", f"--path={INFISICAL_PATH}",
           "--plain", "--silent"]
    if os.environ.get("INFISICAL_PROJECT_ID"):
        cmd.append(f"--projectId={os.environ['INFISICAL_PROJECT_ID']}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print(f"Infisical CLI not installed. Install it and log in, or set {key}.")
        return None
    password = out.stdout.strip()
    if out.returncode != 0 or not password:
        print(f"{key} not found in Infisical ({INFISICAL_ENV} {INFISICAL_PATH}). "
              f"Ask the server team to run ./add-device.sh {device_id}.")
        return None
    return password


def write_device_config(ser, cfg):
    """Replace DEVICE_CONFIG_PATH on the modem with cfg. Never prints the password."""
    data = json.dumps(cfg, separators=(",", ":")).encode()
    folder, name = dev.DEVICE_CONFIG_PATH.rsplit("/", 1)
    send_at(ser, f"AT+FSCD={folder}/")
    send_at(ser, f"AT+FSDEL={name}")   # CFTRANRX needs the file not to exist; ERROR if it doesn't
    print(f"Writing {dev.DEVICE_CONFIG_PATH} ({len(data)} bytes)")
    if dev.send_with_prompt(ser, f'AT+CFTRANRX="{dev.DEVICE_CONFIG_PATH}",{len(data)}', data,
                            result_timeout=10) is None:
        return False
    return dev.read_device_config(ser) == cfg


def read_imei(ser):
    m = re.search(r'\b(\d{15})\b', send_at(ser, "AT+CGSN"))
    return m.group(1) if m else ""


def acceptance_test(ser, imei):
    """Connect with the stored login and leave a retained 'provisioned' state on <id>/up/status."""
    if dev.bring_up_network(ser) is None or not dev.check_clock(ser) or not dev.configure_ssl(ser):
        return False
    if not dev.connect_with_backoff(ser):
        return False
    state = json.dumps({"provisioned": datetime.datetime.now().isoformat(timespec="seconds"), "imei": imei})
    ok = dev.publish(ser, dev.TOPIC_STATUS, state, qos=1, retained=1)
    reset_mqtt(ser)   # Clean disconnect: no last will
    return ok


def record(device_id, imei, result):
    new = not os.path.exists(REGISTRY)
    with open(REGISTRY, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["device_id", "imei", "provisioned_at", "result"])
        w.writerow([device_id, imei, datetime.datetime.now().isoformat(timespec="seconds"), result])


def main():
    parser = argparse.ArgumentParser(description="Store a device's MQTT login on its A7672 modem.")
    parser.add_argument("device_id", help="Lowercase letters, digits and -, e.g. oceo2")
    parser.add_argument("--skip-test", action="store_true", help="Only write the login, don't connect")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z0-9-]+", args.device_id):
        parser.error("device id: lowercase letters, digits and - only")

    password = get_password(args.device_id)
    if not password:
        sys.exit(1)
    cfg = {"type": "mqtt", "host": dev.MQTT_HOST, "port": dev.MQTT_PORT,
           "username": args.device_id, "password": password}

    ser = serial.Serial(dev.PORT, dev.BAUD, timeout=1)
    result = "failed"
    imei = ""
    try:
        if "OK" not in send_at(ser, "AT"):
            print("Module not responding. Check serial connection and power.")
            return
        send_at(ser, "ATE0")
        imei = read_imei(ser)
        print(f"Modem IMEI: {imei or 'unknown'}")

        existing = dev.read_device_config(ser)
        if existing and existing.get("username") != args.device_id:
            print(f"Modem is already provisioned as {existing.get('username')}; overwriting with {args.device_id}")

        if not write_device_config(ser, cfg):
            print("Writing the login to the modem failed (read-back did not match)")
            return
        print("Login stored on the modem")
        result = "stored"

        if not args.skip_test:
            dev.set_device(cfg)
            result = "ok" if acceptance_test(ser, imei) else "stored, test failed"
    finally:
        ser.close()
        record(args.device_id, imei, result)
        print(f"\n{args.device_id} (IMEI {imei or '?'}): {result}  -> recorded in {REGISTRY}")
        if result == "ok":
            print("Device is installed: it now connects on its own, no password needed in the field.")


if __name__ == "__main__":
    main()
