"""Local relay (the app): installs devices, tracks which are online, shows their data, sends commands.

Uses the relay-desktop login. Watches new firmware topics (<id>/up/#) and old ones
(<id>/status, <id>/points) during the rollout; commands go to <id>/down/cmd.
Password from Infisical (project Nodewave, env prod, folder /mqtt), key MQTT_RELAY_DESKTOP_PASSWORD:
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py --device oceo2 --cmd '{"action":"read"}'

Installing a device: register it, then power it on; it is online once its first data arrives:
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py --install dev01
    .venv/bin/python mqtt_relay_local.py --list
"""
import argparse
import datetime
import json
import os
import re
import socket
import ssl
import sys
import time

import paho.mqtt.client as mqtt

MQTT_HOST = "mqtt.nodewave.dev"
MQTT_PORT = 8883
KEEPALIVE = 60
MQTT_USERNAME = "relay-desktop"
MQTT_PASSWORD = os.environ.get("MQTT_RELAY_DESKTOP_PASSWORD", "")
# Unique and stable: two clients with the same id kick each other off
MQTT_CLIENT_ID = f"relay-desktop-{socket.gethostname()}"

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mqtt_relay_log.jsonl")
# Installed devices and their state: pending -> online <-> offline
REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")
SILENT_AFTER = datetime.timedelta(minutes=15)   # No data for this long: shown as silent
INSTALL_TIMEOUT = 600                           # Seconds --install waits for the first data


def load_registry():
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_registry(registry):
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(registry, f, indent=2)
    os.replace(tmp, REGISTRY_FILE)


def register_device(device_id):
    registry = load_registry()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    entry = registry.setdefault(device_id, {"registered_at": now})
    if entry.get("state") != "online":
        entry["state"] = "pending"
    save_registry(registry)
    return entry


def update_device_state(device, kind, payload, retained):
    """Mark a device online on live data, offline on its 'offline' status. Returns the new state."""
    registry = load_registry()
    entry = registry.get(device)
    if entry is None:
        return "unregistered"
    if retained:
        return entry["state"]   # Last known value from the broker, may be old: proves nothing
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if kind in ("up/status", "status") and payload == "offline":
        entry["state"] = "offline"
    else:
        entry["state"] = "online"
        entry.setdefault("installed_at", now)
        entry["last_seen"] = now
    save_registry(registry)
    return entry["state"]


def list_devices():
    registry = load_registry()
    if not registry:
        print(f"No devices registered ({REGISTRY_FILE})")
        return
    now = datetime.datetime.now()
    print(f"{'device':12s} {'state':9s} {'installed':20s} last seen")
    for device, e in sorted(registry.items()):
        state = e["state"]
        last = e.get("last_seen")
        if state == "online" and last and now - datetime.datetime.fromisoformat(last) > SILENT_AFTER:
            state = "silent"
        print(f"{device:12s} {state:9s} {e.get('installed_at', '-'):20s} {last or '-'}")


def decode_payload(raw):
    """Return the payload as JSON if possible, else text, else hex."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "hex", raw.hex()
    try:
        return "json", json.loads(text)
    except ValueError:
        return "text", text


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        print(f"Connect refused: {reason_code} (bad login or not allowed)")
        return
    print(f"Connected to {MQTT_HOST}:{MQTT_PORT} as {MQTT_USERNAME} ({MQTT_CLIENT_ID})")
    # Subscribing here re-subscribes after every reconnect
    device = userdata["device"] or userdata.get("install") or "+"
    client.subscribe([(f"{device}/up/#", 1),                              # new firmware
                      (f"{device}/status", 1), (f"{device}/points", 1)])  # old firmware

    cmd = userdata.get("cmd")
    if cmd and not userdata.get("cmd_sent"):
        topic = f"{userdata['device']}/down/cmd"
        client.publish(topic, cmd, qos=1, retain=False)
        userdata["cmd_sent"] = True
        print(f"Sent command to {topic}: {cmd}")


def on_subscribe(client, userdata, mid, reason_codes, properties):
    for rc in reason_codes:
        if rc.is_failure:
            print(f"Subscribe refused: {rc}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    print(f"Disconnected: {reason_code} (will reconnect with back-off)")


def on_message(client, userdata, msg):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    device, _, kind = msg.topic.partition("/")
    fmt, payload = decode_payload(msg.payload)
    # Retained status is the last known state and may be old
    tag = " [retained, may be old]" if msg.retain else ""
    shown = json.dumps(payload) if fmt == "json" else payload
    print(f"{now}  {device:10s} {kind:10s} qos{msg.qos} {len(msg.payload):4d}B {fmt}{tag}: {shown[:300]}")

    before = load_registry().get(device, {}).get("state")
    state = update_device_state(device, kind, payload, msg.retain)
    if state == "unregistered":
        print(f"  {device} is not installed: register it with --install {device}")
    elif state != before:
        print(f"  {device}: {before} -> {state}")
    if userdata.get("install") == device and state == "online":
        print(f"\n{device} is installed and online.")
        userdata["installed"] = True
        client.disconnect()

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "received_at": now,
            "topic": msg.topic,
            "device": device,
            "kind": kind,
            "qos": msg.qos,
            "retained": bool(msg.retain),
            "format": fmt,
            "payload": payload,
        }) + "\n")


def send_once(device, cmd):
    """Connect, send one command to <device>/down/cmd, wait for the broker's ack and disconnect."""
    # Own client id so it doesn't kick off a watching relay
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{MQTT_CLIENT_ID}-cmd")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
    client.loop_start()
    topic = f"{device}/down/cmd"
    info = client.publish(topic, cmd, qos=1, retain=False)
    info.wait_for_publish(timeout=15)
    print(f"Sent command to {topic}: {cmd}" if info.is_published() else f"Command to {topic} not acknowledged")
    client.disconnect()
    client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description="Watch device status/points on the Nodewave broker.")
    parser.add_argument("--device", help="Only watch this device id (default: all devices)")
    parser.add_argument("--cmd", help="Send this payload to <device>/down/cmd once connected (needs --device)")
    parser.add_argument("--once", action="store_true", help="With --cmd: send it and exit, without watching")
    parser.add_argument("--install", metavar="DEVICE_ID",
                        help="Register a device and wait until its first data arrives (= online)")
    parser.add_argument("--list", action="store_true", help="Show installed devices and their state")
    args = parser.parse_args()

    if args.list:
        list_devices()
        return
    if args.install:
        if not re.fullmatch(r"[a-z0-9-]+", args.install):
            parser.error("device id: lowercase letters, digits and - only")
        register_device(args.install)

    if not MQTT_PASSWORD:
        print("Set MQTT_RELAY_DESKTOP_PASSWORD (from Infisical, project Nodewave, prod, /mqtt).")
        sys.exit(1)
    if args.cmd and not args.device:
        parser.error("--cmd needs --device")
    if args.once and not args.cmd:
        parser.error("--once needs --cmd")

    if args.once:
        send_once(args.device, args.cmd)
        return

    userdata = {"device": args.device, "cmd": args.cmd, "install": args.install}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID, userdata=userdata)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)   # System CAs include ISRG Root X1
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    print(f"Logging messages to {LOG_FILE}")
    if args.install:
        print(f"{args.install} registered as pending. Power on the device; waiting up to "
              f"{INSTALL_TIMEOUT // 60} min for its first data...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
    try:
        if args.install:
            client.loop_start()
            deadline = time.time() + INSTALL_TIMEOUT
            while not userdata.get("installed") and time.time() < deadline:
                time.sleep(0.5)
            client.loop_stop()
            if not userdata.get("installed"):
                print(f"\nNo data from {args.install} yet; it stays pending. Check the device, then run --list.")
                sys.exit(1)
        else:
            client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        print("Exiting...")
        client.disconnect()


if __name__ == "__main__":
    main()
