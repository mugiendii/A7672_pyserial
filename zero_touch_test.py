"""Zero-touch install over AT commands: what the MCU firmware will do (broker repo PROVISIONING.md).

    MQTT_PROVISION_BOOTSTRAP_PASSWORD=... .venv/bin/python zero_touch_test.py

1. No login on the modem yet: connect with the shared first-boot login `provision` as client id
   imei-<IMEI>, publish a claim on provision/imei-<IMEI>/up every minute, listen on .../down.
2. Approve the IMEI in the app (or POST /v1/claims/<IMEI>/approve {"id": "..."}).
3. The server sends {"id","password","host","port"} on provision/imei-<IMEI>/down: save it to the
   modem's flash (C:/mqtt.json), drop the provision login.
4. Reconnect as the device id, publish <id>/up/status = online (closes the claim on the server) and
   some <id>/up/points: the app sees the data and the device is online.
Next boots skip 1-3 because C:/mqtt.json exists (--reclaim forgets it and starts over).
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

import serial

import mqtt_tls_test as dev
from provision_device import read_imei, write_device_config
from setup import send_at, reset_mqtt

PROVISION_USERNAME = "provision"
PROVISION_PASSWORD = os.environ.get("MQTT_PROVISION_BOOTSTRAP_PASSWORD", "")
FW_VERSION = "at-test-0.1"
CLAIM_INTERVAL = 60      # Seconds between claims while waiting for approval
APPROVAL_TIMEOUT = 1800  # Give up waiting for approval after this long
TEST_POINTS = 3


def wait_for_login(ser, claim_topic, reply_topic):
    """Publish a claim every CLAIM_INTERVAL until the server replies. Returns the reply dict or None."""
    claim = json.dumps({"fw": FW_VERSION})
    deadline = time.time() + APPROVAL_TIMEOUT
    next_claim = 0
    buf = ""
    while time.time() < deadline:
        if time.time() >= next_claim:
            if not dev.publish(ser, claim_topic, claim, qos=1):
                return None
            next_claim = time.time() + CLAIM_INTERVAL
            print("Claim sent. Approve this IMEI in the app; waiting...")
            # The server answers an approved claim at once: the reply may have come with the ack
            buf += dev.take_rx_backlog()
        if ser.in_waiting > 0 or "+CMQTTRXEND:" in buf:
            buf += ser.read(ser.in_waiting).decode(errors='ignore')
            while "+CMQTTRXEND:" in buf:
                block, _, buf = buf.partition("+CMQTTRXEND:")
                buf = buf.split("\n", 1)[1] if "\n" in buf else ""
                topic, payload = dev.parse_rx(block)
                if topic != reply_topic:
                    continue
                try:
                    reply = json.loads(payload)
                except ValueError:
                    print("Reply is not JSON, ignoring")
                    continue
                if reply.get("id") and reply.get("password"):
                    print(f"Login received: id = {reply['id']}")   # Never print the password
                    return reply
            if "+CMQTTCONNLOST:" in buf:
                print("Connection lost while waiting for approval")
                return None
        time.sleep(0.1)
    print("No approval in time")
    return None


def claim_login(ser, imei):
    """Steps 1-3. Returns the stored login dict or None."""
    cid = f"imei-{imei}"
    dev.set_device({"username": PROVISION_USERNAME, "password": PROVISION_PASSWORD}, client_id=cid)
    if not dev.connect_with_backoff(ser, will=False):
        return None
    reply_topic = f"provision/{cid}/down"
    if not dev.subscribe(ser, reply_topic):
        return None
    reply = wait_for_login(ser, f"provision/{cid}/up", reply_topic)
    reset_mqtt(ser)
    if not reply:
        return None
    cfg = {"type": "mqtt", "host": reply.get("host", dev.MQTT_HOST), "port": reply.get("port", dev.MQTT_PORT),
           "username": reply["id"], "password": reply["password"]}
    if not write_device_config(ser, cfg):
        print("Saving the login to the modem failed")
        return None
    print(f"Login saved to {dev.DEVICE_CONFIG_PATH}")
    return cfg


def forget_login(ser):
    folder, name = dev.DEVICE_CONFIG_PATH.rsplit("/", 1)
    send_at(ser, f"AT+FSCD={folder}/")
    send_at(ser, f"AT+FSDEL={name}")


def main():
    parser = argparse.ArgumentParser(description="Zero-touch device install over A7672 AT commands.")
    parser.add_argument("--reclaim", action="store_true", help="Forget the stored login and claim again")
    args = parser.parse_args()

    ser = serial.Serial(dev.PORT, dev.BAUD, timeout=1)
    results = {}
    connected = False
    try:
        if "OK" not in send_at(ser, "AT"):
            print("Module not responding. Check serial connection and power.")
            return
        send_at(ser, "ATE0")
        imei = read_imei(ser)
        if not imei:
            print("Could not read the IMEI")
            return
        print(f"\n*** IMEI {imei} ***\n")
        if args.reclaim:
            forget_login(ser)

        results["network"] = dev.bring_up_network(ser) is not None
        if not results["network"]:
            return
        results["clock"] = dev.check_clock(ser)
        if not results["clock"]:
            return
        results["ssl_config"] = dev.configure_ssl(ser)
        if not results["ssl_config"]:
            return

        cfg = dev.read_device_config(ser)
        if cfg:
            print(f"Modem already has a login for {cfg['username']}; skipping the claim")
        else:
            if not PROVISION_PASSWORD:
                print("Set MQTT_PROVISION_BOOTSTRAP_PASSWORD (Infisical, Nodewave / prod / /mqtt).")
                return
            cfg = claim_login(ser, imei)
            results["claim"] = cfg is not None
            if not cfg:
                return

        dev.set_device(cfg)
        results["connect"] = connected = dev.connect_with_backoff(ser)
        if not connected:
            return
        results["status_online"] = dev.publish(ser, dev.TOPIC_STATUS, "online", qos=1, retained=1)
        results["sub_down"] = dev.subscribe(ser, dev.TOPIC_DOWN)
        sent = 0
        for i in range(TEST_POINTS):
            payload = json.dumps({"test": "zero-touch", "seq": i + 1,
                                  "timestamp": datetime.datetime.now().isoformat()})
            if dev.publish(ser, dev.TOPIC_POINTS, payload, qos=1):
                sent += 1
            time.sleep(2)
        results["points"] = f"{sent}/{TEST_POINTS}"
        results["received"] = dev.listen(ser, 30)
    except KeyboardInterrupt:
        print("Interrupted")
    finally:
        if connected:
            dev.publish(ser, dev.TOPIC_STATUS, "offline", qos=1, retained=1)
        reset_mqtt(ser)
        ser.close()
        print("\n=== ZERO-TOUCH TEST RESULTS ===")
        for step, ok in results.items():
            print(f"{step:14s}: {ok}")


if __name__ == "__main__":
    main()
