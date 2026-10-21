"""Install a device on the platform: store its MQTT login on the modem once, on the bench.

The device keeps its login in the modem's flash (C:/mqtt.json) next to the CA certificate, so nobody
needs the password again after this. Run once per device, with the modem on /dev/ttyUSB0:
    .venv/bin/python provision_device.py oceo2

The login comes from the provisioning API (https://mqtt-api.nodewave.dev, broker repo PROVISIONING.md):
this script asks it to create the device (or give an existing one a new password) and writes the
answer straight onto the modem. Nobody sees or copies the password. Needs the app's API key once:
    MQTT_PROVISION_API_KEY=... .venv/bin/python provision_device.py dev01
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

import serial

import mqtt_tls_test as dev
from setup import send_at, reset_mqtt

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.csv")
PROVISION_API = os.environ.get("MQTT_PROVISION_API_URL", "https://mqtt-api.nodewave.dev").rstrip("/")
PROVISION_API_KEY = os.environ.get("MQTT_PROVISION_API_KEY", "")


def api(method, path, body=None):
    """Call the provisioning API. Returns (status, json body)."""
    req = urllib.request.Request(
        PROVISION_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {PROVISION_API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {"error": e.reason}


def get_login(device_id):
    """Create the device on the broker, or give an existing one a new password. Never printed."""
    if not PROVISION_API_KEY:
        print("Set MQTT_PROVISION_API_KEY (the app's key for the provisioning API).")
        return None
    status, body = api("POST", "/v1/devices", {"id": device_id})
    if status == 409:
        print(f"{device_id} already exists on the broker: giving it a new password")
        status, body = api("POST", f"/v1/devices/{device_id}/rotate")
    if status not in (200, 201):
        print(f"Provisioning API refused ({status}): {body.get('error')}")
        return None
    return {"type": "mqtt", "host": body["broker"]["host"], "port": body["broker"]["port"],
            "username": body["username"], "password": body["password"]}


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

    # Check the modem first: a new password is only worth making if it can be stored
    ser = serial.Serial(dev.PORT, dev.BAUD, timeout=1)
    if "OK" not in send_at(ser, "AT"):
        print("Module not responding. Check serial connection and power.")
        ser.close()
        sys.exit(1)
    cfg = get_login(args.device_id)
    if not cfg:
        ser.close()
        sys.exit(1)

    result = "failed"
    imei = ""
    try:
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
