"""Local relay to watch device data on the production broker (and optionally send a command).

Uses the relay-desktop login: reads +/status, +/points; writes +/cmd, +/cfg.
Password from Infisical (project Nodewave, env prod, folder /mqtt), key MQTT_RELAY_DESKTOP_PASSWORD:
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py
    MQTT_RELAY_DESKTOP_PASSWORD=... .venv/bin/python mqtt_relay_local.py --device oceo2 --cmd '{"action":"read"}'
"""
import argparse
import datetime
import json
import os
import socket
import ssl
import sys

import paho.mqtt.client as mqtt

MQTT_HOST = "mqtt.nodewave.dev"
MQTT_PORT = 8883
KEEPALIVE = 60
MQTT_USERNAME = "relay-desktop"
MQTT_PASSWORD = os.environ.get("MQTT_RELAY_DESKTOP_PASSWORD", "")
# Unique and stable: two clients with the same id kick each other off
MQTT_CLIENT_ID = f"relay-desktop-{socket.gethostname()}"

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mqtt_relay_log.jsonl")


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
    device = userdata["device"] or "+"
    client.subscribe([(f"{device}/status", 1), (f"{device}/points", 1)])

    cmd = userdata.get("cmd")
    if cmd and not userdata.get("cmd_sent"):
        topic = f"{userdata['device']}/cmd"
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
    print(f"{now}  {device:10s} {kind:7s} qos{msg.qos} {len(msg.payload):4d}B {fmt}{tag}: {shown[:300]}")

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
    """Connect, send one command to <device>/cmd, wait for the broker's ack and disconnect."""
    # Own client id so it doesn't kick off a watching relay
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{MQTT_CLIENT_ID}-cmd")
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
    client.loop_start()
    topic = f"{device}/cmd"
    info = client.publish(topic, cmd, qos=1, retain=False)
    info.wait_for_publish(timeout=15)
    print(f"Sent command to {topic}: {cmd}" if info.is_published() else f"Command to {topic} not acknowledged")
    client.disconnect()
    client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description="Watch device status/points on the Nodewave broker.")
    parser.add_argument("--device", help="Only watch this device id (default: all devices)")
    parser.add_argument("--cmd", help="Send this payload to <device>/cmd once connected (needs --device)")
    parser.add_argument("--once", action="store_true", help="With --cmd: send it and exit, without watching")
    args = parser.parse_args()

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

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID,
                         userdata={"device": args.device, "cmd": args.cmd})
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)   # System CAs include ISRG Root X1
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    print(f"Logging messages to {LOG_FILE}")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=KEEPALIVE)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        print("Exiting...")
        client.disconnect()


if __name__ == "__main__":
    main()
