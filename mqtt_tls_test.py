"""Production MQTT (TLS, port 8883) test for the A7672, following the Nodewave broker guide.

Uses the login stored on the modem by provision_device.py (C:/mqtt.json), like the device firmware does:
    .venv/bin/python mqtt_tls_test.py
Override for a modem that is not provisioned:
    MQTT_DEVICE_ID=oceo2 MQTT_DEVICE_OCEO2_PASSWORD=... .venv/bin/python mqtt_tls_test.py
"""
import os
import re
import sys
import time
import datetime
import json

import serial

from setup import send_at, wait_for_urc, reset_mqtt

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'
BAUD = 115200
SIM_PIN = "3759"
APN_NAME = "safaricom"

# --- PRODUCTION MQTT (TLS) CONFIGURATION ---
MQTT_HOST = "mqtt.nodewave.dev"   # Always the hostname, never the IP
MQTT_PORT = 8883
KEEPALIVE = 60

# Broker settings + login stored on the modem's flash; same shape as a {"type":"mqtt"} <id>/down/cfg message
DEVICE_CONFIG_PATH = "C:/mqtt.json"

# Set by set_device(): device id = username = client id = topic prefix
DEVICE_ID = MQTT_CLIENT_ID = MQTT_PASSWORD = MQTT_BROKER_URI = ""
TOPIC_STATUS = TOPIC_POINTS = TOPIC_DOWN = TOPIC_CMD = TOPIC_CFG = ""

# --- TLS CONFIGURATION ---
SSL_CTX = 0
CA_CERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "isrgrootx1.pem")
CA_CERT_NAME = "isrgrootx1.pem"   # Filename stored on the module

# Reconnect back-off: 1 s, 2 s, 4 s ... capped at 60 s (broker blocks >20 new connections/min per IP)
CONNECT_ATTEMPTS = 4
BACKOFF_MAX = 60

TEST_POINTS = 3
LISTEN_SECONDS = 60


def set_device(cfg, client_id=None):
    """Use the broker settings and login in cfg ({"host","port","username","password"})."""
    global DEVICE_ID, MQTT_CLIENT_ID, MQTT_PASSWORD, MQTT_BROKER_URI
    global TOPIC_STATUS, TOPIC_POINTS, TOPIC_DOWN, TOPIC_CMD, TOPIC_CFG
    DEVICE_ID = cfg["username"]
    MQTT_CLIENT_ID = client_id or DEVICE_ID
    MQTT_PASSWORD = cfg["password"]
    MQTT_BROKER_URI = f"tcp://{cfg.get('host', MQTT_HOST)}:{cfg.get('port', MQTT_PORT)}"
    # Device publishes under <id>/up/, reads <id>/down/ (anything else is silently dropped by the broker)
    TOPIC_STATUS = f"{DEVICE_ID}/up/status"
    TOPIC_POINTS = f"{DEVICE_ID}/up/points"
    TOPIC_DOWN = f"{DEVICE_ID}/down/#"
    TOPIC_CMD = f"{DEVICE_ID}/down/cmd"
    TOPIC_CFG = f"{DEVICE_ID}/down/cfg"


def read_device_config(ser):
    """Read DEVICE_CONFIG_PATH from the modem. Returns the dict or None. Never prints the password."""
    ser.reset_input_buffer()
    ser.write(f'AT+CFTRANTX="{DEVICE_CONFIG_PATH}"\r\n'.encode())
    resp = wait_for_urc(ser, "+CFTRANTX: 0", timeout=5)
    m = re.search(r'\+CFTRANTX: DATA, ?(\d+)\r\n', resp or "")
    if not m:
        return None
    try:
        return json.loads(resp[m.end():m.end() + int(m.group(1))])
    except ValueError:
        print(f"{DEVICE_CONFIG_PATH} on the modem is not valid JSON")
        return None


def bring_up_network(ser):
    """Register on the network and activate the PDP context. Returns IP or None."""
    if "OK" not in send_at(ser, "AT"):
        print("Module not responding. Check serial connection and power.")
        return None
    send_at(ser, "ATE0")

    if "SIM PIN" in send_at(ser, "AT+CPIN?"):
        send_at(ser, f'AT+CPIN="{SIM_PIN}"')
        time.sleep(5)

    send_at(ser, "AT+CTZU=1")   # Take time from the network
    send_at(ser, "AT+CFUN=1")

    for _ in range(10):
        reg = send_at(ser, "AT+CREG?")
        if "+CREG: 0,1" in reg or "+CREG: 0,5" in reg:
            break
        time.sleep(3)
    else:
        print("Network registration failed")
        return None

    send_at(ser, "AT+CGATT=1")
    send_at(ser, f'AT+CGDCONT=1,"IP","{APN_NAME}"')
    send_at(ser, "AT+CGACT=1,1")

    ip_response = send_at(ser, "AT+CGPADDR=1")
    if "+CGPADDR: 1," not in ip_response:
        print("Failed to get IP address")
        return None
    ip = ip_response.split(',')[1].split()[0].strip()
    print(f"IP Address: {ip}")
    return ip


def check_clock(ser, timeout=60):
    """Wait for a sane module clock. A clock at 1980/2000 fails every TLS handshake."""
    end = time.time() + timeout
    while time.time() < end:
        m = re.search(r'\+CCLK: "(\d\d)/', send_at(ser, "AT+CCLK?"))
        if m and 2000 + int(m.group(1)) >= 2025:
            return True
        print("Clock not synced yet, waiting for network time...")
        time.sleep(5)
    print("Module clock is wrong: TLS certificate checks will fail")
    return False


def send_with_prompt(ser, command, data, result_timeout=5):
    """Send a command that answers with '>', then the data. Returns the response on OK, else None."""
    ser.write((command + '\r\n').encode())
    if not wait_for_urc(ser, ">", timeout=5):
        print(f"{command}: prompt timeout")
        return None
    ser.write(data.encode() if isinstance(data, str) else data)
    resp = wait_for_urc(ser, "OK", timeout=result_timeout)
    if not resp:
        print(f"{command}: no OK")
        return None
    return resp


def configure_ssl(ser):
    """TLS 1.2, verify the server against ISRG Root X1, SNI on."""
    print("Configuring SSL context...")
    with open(CA_CERT_PATH, 'rb') as f:
        ca = f.read()
    if not send_with_prompt(ser, f'AT+CCERTDOWN="{CA_CERT_NAME}",{len(ca)}', ca, result_timeout=10):
        print("CA cert upload failed")
        return False
    send_at(ser, "AT+CCERTLIST")

    for cmd in (
        f'AT+CSSLCFG="sslversion",{SSL_CTX},3',          # TLS 1.2
        f'AT+CSSLCFG="authmode",{SSL_CTX},1',            # Verify the server
        f'AT+CSSLCFG="cacert",{SSL_CTX},"{CA_CERT_NAME}"',
        f'AT+CSSLCFG="ignorelocaltime",{SSL_CTX},0',     # Check cert validity against the clock
        f'AT+CSSLCFG="enableSNI",{SSL_CTX},1',
    ):
        if "OK" not in send_at(ser, cmd):
            print(f"SSL config failed: {cmd}")
            return False
    return True


def connect_once(ser):
    """One connect attempt. Returns the +CMQTTCONNECT error code (0 = accepted) or None."""
    reset_mqtt(ser)

    resp = send_at(ser, "AT+CMQTTSTART", wait_time=2)
    if "+CMQTTSTART: 0" not in resp and not wait_for_urc(ser, "+CMQTTSTART: 0", timeout=5):
        print("MQTT start failed")
        return None

    # server_type 1 = MQTT over SSL/TLS
    if "OK" not in send_at(ser, f'AT+CMQTTACCQ=0,"{MQTT_CLIENT_ID}",1'):
        print("MQTT client acquire failed")
        return None
    if "OK" not in send_at(ser, f'AT+CMQTTSSLCFG=0,{SSL_CTX}'):
        print("Binding SSL context to MQTT client failed")
        return None

    send_at(ser, 'AT+CMQTTCFG="version",0,4')   # MQTT 3.1.1
    send_at(ser, 'AT+CMQTTCFG="optimeout",0,60')

    # Last will: <id>/up/status = offline, QoS 1.
    # AT+CMQTTWILLMSG has no retain flag on this firmware, so the will is not retained.
    if not send_with_prompt(ser, f'AT+CMQTTWILLTOPIC=0,{len(TOPIC_STATUS)}', TOPIC_STATUS):
        return None
    if not send_with_prompt(ser, f'AT+CMQTTWILLMSG=0,{len("offline")},1', "offline"):
        return None

    # Clean session = 1
    # Written directly so the password is not printed
    print(f'Sending: AT+CMQTTCONNECT=0,"{MQTT_BROKER_URI}",{KEEPALIVE},1,"{DEVICE_ID}","********"')
    ser.write(f'AT+CMQTTCONNECT=0,"{MQTT_BROKER_URI}",{KEEPALIVE},1,"{DEVICE_ID}","{MQTT_PASSWORD}"\r\n'.encode())
    resp = wait_for_urc(ser, "OK", timeout=5) or ""
    if "ERROR" in resp:
        print("MQTT connect command failed")
        return None
    m = re.search(r'\+CMQTTCONNECT: 0,(\d+)', resp)
    if not m:
        # TLS handshake takes longer than plain TCP
        urc = wait_for_urc(ser, "+CMQTTCONNECT:", timeout=30) or ""
        m = re.search(r'\+CMQTTCONNECT: 0,(\d+)', urc)
    if not m:
        print("MQTT connect: no result (timeout)")
        return None
    return int(m.group(1))


def connect_with_backoff(ser):
    delay = 1
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        print(f"Connect attempt {attempt}/{CONNECT_ATTEMPTS} to {MQTT_BROKER_URI} as {DEVICE_ID}...")
        err = connect_once(ser)
        if err == 0:
            print(f"MQTT connected over TLS to {MQTT_BROKER_URI}")
            return True
        if err in (4, 5):
            print(f"Connect refused ({err}): wrong username/password or not allowed. Not retrying.")
            return False
        print(f"Connect failed (code {err}). A TLS error almost always means a wrong clock or missing CA.")
        if attempt < CONNECT_ATTEMPTS:
            print(f"Backing off {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)
    return False


def subscribe(ser, topic):
    if send_with_prompt(ser, f'AT+CMQTTSUBTOPIC=0,{len(topic)},1', topic) is None:
        return False
    urc = send_at(ser, "AT+CMQTTSUB=0")
    if "+CMQTTSUB:" not in urc:
        urc = wait_for_urc(ser, "+CMQTTSUB:", timeout=10)
    if urc and "+CMQTTSUB: 0,0" in urc:
        print(f"Subscribed to {topic}")
        return True
    print(f"Subscribe result: {urc}")
    return False


def publish(ser, topic, payload, qos=1, retained=0):
    """Publish one message. Returns True when the broker acknowledged it."""
    if not send_with_prompt(ser, f'AT+CMQTTTOPIC=0,{len(topic)}', topic):
        return False
    if not send_with_prompt(ser, f'AT+CMQTTPAYLOAD=0,{len(payload.encode())}', payload):
        return False
    resp = send_at(ser, f'AT+CMQTTPUB=0,{qos},60,{retained}')
    if "+CMQTTPUB: 0,0" not in resp:
        urc = wait_for_urc(ser, "+CMQTTPUB:", timeout=15) or ""
        resp += urc
    if "+CMQTTPUB: 0,0" in resp:
        print(f"Published to {topic}: {payload}")
        return True
    print(f"Publish to {topic} failed: {resp}")
    return False


def parse_rx(block):
    """Rebuild (topic, payload) from one +CMQTTRXSTART ... +CMQTTRXEND block (parts may be split)."""
    def collect(prefix):
        out = ""
        for m in re.finditer(rf'\+{prefix}: 0,(\d+)\r\n', block):
            out += block[m.end():m.end() + int(m.group(1))]
        return out
    return collect("CMQTTRXTOPIC"), collect("CMQTTRXPAYLOAD")


def listen(ser, seconds):
    print(f"Listening on {TOPIC_DOWN} for {seconds}s (send one with an app login, e.g. relay-desktop)...")
    end = time.time() + seconds
    buf = ""
    received = 0
    while time.time() < end:
        if ser.in_waiting > 0:
            buf += ser.read(ser.in_waiting).decode(errors='ignore')
            while "+CMQTTRXEND:" in buf:
                block, _, buf = buf.partition("+CMQTTRXEND:")
                buf = buf.split("\n", 1)[1] if "\n" in buf else ""
                topic, payload = parse_rx(block)
                received += 1
                if topic == TOPIC_CMD:
                    print(f"Command received: {payload}")
                elif topic == TOPIC_CFG:
                    print(f"Config received: {payload}")
                else:
                    print(f"Ignoring unknown topic {topic}: {payload}")
            if "+CMQTTCONNLOST:" in buf:
                print(f"Connection lost:\n{buf.strip()}")
                break
        time.sleep(0.1)
    return received


def main():
    ser = serial.Serial(PORT, BAUD, timeout=1)

    device_id = os.environ.get("MQTT_DEVICE_ID")
    password = device_id and os.environ.get(f"MQTT_DEVICE_{device_id.upper().replace('-', '_')}_PASSWORD")
    if device_id and password:
        cfg = {"username": device_id, "password": password}
    else:
        cfg = read_device_config(ser)
        if not cfg:
            print(f"Modem has no {DEVICE_CONFIG_PATH}: run provision_device.py first, "
                  f"or set MQTT_DEVICE_ID and MQTT_DEVICE_<ID>_PASSWORD.")
            ser.close()
            sys.exit(1)
        print(f"Using login for {cfg['username']} from the modem ({DEVICE_CONFIG_PATH})")
    # Override (e.g. "oceo2-test") to avoid kicking off a live device with the same id
    set_device(cfg, os.environ.get("MQTT_CLIENT_ID"))

    results = {}
    connected = False
    try:
        results["network"] = bring_up_network(ser) is not None
        if not results["network"]:
            return
        results["clock"] = check_clock(ser)
        if not results["clock"]:
            return
        results["ssl_config"] = configure_ssl(ser)
        if not results["ssl_config"]:
            return
        results["connect"] = connected = connect_with_backoff(ser)
        if not connected:
            return

        results["status_online"] = publish(ser, TOPIC_STATUS, "online", qos=1, retained=1)
        results["sub_down"] = subscribe(ser, TOPIC_DOWN)

        sent = 0
        for i in range(TEST_POINTS):
            payload = json.dumps({
                "test": "tls",
                "seq": i + 1,
                "timestamp": datetime.datetime.now().isoformat(),
            })
            if publish(ser, TOPIC_POINTS, payload, qos=1):
                sent += 1
            time.sleep(2)
        results["points"] = f"{sent}/{TEST_POINTS}"

        results["received"] = listen(ser, LISTEN_SECONDS)
    except KeyboardInterrupt:
        print("Interrupted")
    finally:
        if connected:
            publish(ser, TOPIC_STATUS, "offline", qos=1, retained=1)
        reset_mqtt(ser)
        ser.close()
        print("\n=== TLS MQTT TEST RESULTS ===")
        for step, ok in results.items():
            print(f"{step:14s}: {ok}")
        print("Check Grafana -> MQTT (https://monitor.nodewave.dev) to confirm the broker saw the login and messages.")


if __name__ == "__main__":
    main()
