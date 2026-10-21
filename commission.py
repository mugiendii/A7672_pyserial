"""Device commissioning simulator: what the platform app and the device do together.

    python3 mqtt_tls_test.py          (module on /dev/ttyUSB0; opens http://127.0.0.1:8080)

1. The page shows the module (IMEI). The installer types a name and clicks Install.
2. This app asks the provisioning API for that device's login and writes it onto the module
   with AT commands (C:/mqtt.json). Nobody sees the password.
3. The module connects over TLS and sends a dummy reading every 10 s, until stopped.
4. The page also shows the readings arriving on the relay side (login relay-desktop).

The app's own keys (MQTT_PROVISION_API_KEY, MQTT_RELAY_DESKTOP_PASSWORD) are set up on the first
run: fetched from the server over SSH into ~/.config/nodewave.env. The installer never handles them.
"""
import datetime
import html
import json
import os
import queue
import re
import ssl
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER = "root@157.173.107.18"
KEYS = ("MQTT_PROVISION_API_KEY", "MQTT_RELAY_DESKTOP_PASSWORD")
KEY_FILES = (os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodewave.env"),
             os.path.expanduser("~/.config/nodewave.env"))
HTTP_HOST, HTTP_PORT = "127.0.0.1", 8080   # local only: the page can create device logins
EVERY = 10                                 # seconds between dummy readings
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")


# ---- App keys ---------------------------------------------------------------

def load_keys():
    """Put the app's keys in os.environ: from a key file, else fetched from the server once."""
    for path in KEY_FILES:
        if os.path.exists(path):
            _read_env(path)
            if all(os.environ.get(k) for k in KEYS):
                return True
    print(f"First run: fetching the app keys from {SERVER} over SSH...")
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SERVER,
             "/root/mqtt/provision/export-env.sh", *KEYS],
            capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        out = None
    if not out or out.returncode != 0 or not all(k + "=" in out.stdout for k in KEYS):
        print("Could not fetch the app keys over SSH (no SSH access to the server from here?).\n"
              f"Put {', '.join(KEYS)} in {KEY_FILES[1]} (NAME=value lines) and run again.")
        return False
    path = KEY_FILES[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(out.stdout)
    print(f"App keys saved in {path} (readable only by you)")
    _read_env(path)
    return True


def _read_env(path):
    with open(path) as f:
        for line in f:
            name, sep, value = line.strip().partition("=")
            if sep and name in KEYS and value:
                os.environ[name] = value


# ---- State shown on the page ---------------------------------------------------

lock = threading.Lock()
state = {"imei": "", "device": "", "phase": "starting", "log": [], "sent": [], "relay": []}
requests = queue.Queue()   # install requests from the page, handled by the main thread


def say(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}")
    with lock:
        state["log"] = (state["log"] + [f"{stamp}  {msg}"])[-40:]


def set_state(**kw):
    with lock:
        state.update(kw)


def remember(key, item):
    with lock:
        state[key] = ([item] + state[key])[:15]


# ---- Relay side: watch what arrives at the broker ------------------------------

def start_relay_watch():
    import paho.mqtt.client as mqtt
    import socket

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe("+/up/#", qos=1)
            say("Relay: connected as relay-desktop, watching +/up/#")
        else:
            say(f"Relay: login refused ({reason_code})")

    def on_message(client, userdata, msg):
        with lock:
            device = state["device"]
        if device and msg.topic.startswith(device + "/"):
            remember("relay", {"at": datetime.datetime.now().strftime("%H:%M:%S"),
                               "topic": msg.topic,
                               "payload": msg.payload.decode(errors="replace")[:300]})

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"relay-desktop-{socket.gethostname()}-commission")
    c.username_pw_set("relay-desktop", os.environ["MQTT_RELAY_DESKTOP_PASSWORD"])
    c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    c.reconnect_delay_set(min_delay=2, max_delay=60)
    c.on_connect, c.on_message = on_connect, on_message
    c.connect_async("mqtt.nodewave.dev", 8883, keepalive=60)
    c.loop_start()


# ---- Install page ---------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Install device</title>
<style>
 body{font:15px system-ui,sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#1d232b}
 main{max-width:760px;margin:auto} h1{font-size:20px} .card{background:#fff;border:1px solid #dde1e6;
 border-radius:8px;padding:16px;margin:12px 0} input{font:inherit;padding:8px;border:1px solid #b9c0c8;
 border-radius:6px;width:220px} button{font:inherit;padding:8px 16px;border:0;border-radius:6px;
 background:#1f6feb;color:#fff;cursor:pointer} button:disabled{background:#9aa5b1}
 .phase{font-weight:600} pre{white-space:pre-wrap;font:12px ui-monospace,monospace;margin:0;
 max-height:220px;overflow:auto} table{border-collapse:collapse;width:100%;font-size:13px}
 td{border-top:1px solid #eef0f3;padding:4px 6px;vertical-align:top;word-break:break-all}
 .err{color:#b42318}
</style></head><body><main>
<h1>Install device</h1>
<div class="card">Module IMEI <b id="imei">…</b> · device <b id="device">not installed</b><br>
 Status: <span class="phase" id="phase">…</span></div>
<div class="card"><form id="f">Device name
 <input id="name" placeholder="e.g. temp-001" pattern="[a-z0-9][a-z0-9-]{1,30}" required>
 <button id="b">Install</button> <span id="msg"></span></form>
 <small>Lowercase letters, digits and -. Installing again gives the module a new name.</small></div>
<div class="card"><b>Sent by the device</b><table id="sent"></table></div>
<div class="card"><b>Received on the relay side</b><table id="relay"></table></div>
<div class="card"><b>Log</b><pre id="log"></pre></div>
</main><script>
const $=id=>document.getElementById(id);
const rows=(t,l)=>$(t).innerHTML=l.map(r=>`<tr><td>${r.at}</td><td>${r.topic}</td><td>${r.payload}</td></tr>`).join('')||'<tr><td>nothing yet</td></tr>';
async function poll(){try{const s=await (await fetch('/state')).json();
 $('imei').textContent=s.imei||'?';$('device').textContent=s.device||'not installed';
 $('phase').textContent=s.phase;$('log').textContent=s.log.slice().reverse().join('\\n');
 rows('sent',s.sent);rows('relay',s.relay);$('b').disabled=s.phase.startsWith('installing');}catch(e){}}
$('f').onsubmit=async e=>{e.preventDefault();$('msg').textContent='';
 const r=await fetch('/install',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({id:$('name').value.trim()})});
 if(!r.ok){$('msg').innerHTML='<span class=err>'+(await r.json()).error+'</span>'}poll()};
poll();setInterval(poll,2000);
</script></body></html>"""


class Page(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/state":
            with lock:
                view = {k: v for k, v in state.items()}
            # Escape for the page: payloads come from the device
            for key in ("sent", "relay"):
                view[key] = [{k: html.escape(v) for k, v in r.items()} for r in view[key]]
            return self._send(200, view)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/install":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        device_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(device_id, str) or not ID_RE.match(device_id):
            return self._send(400, {"error": "name: lowercase letters, digits and -, 2-31 characters"})
        requests.put(device_id)
        set_state(phase=f"installing {device_id}...")
        self._send(202, {"ok": True})

    def log_message(self, fmt, *args):
        pass


# ---- Device side (main thread: the only one that talks to the module) -----------

def install(ser, dev, prov, device_id):
    """Get the login from the API, write it onto the module, use it. Returns True when done."""
    say(f"Installing as {device_id}: asking the provisioning API for its login")
    cfg = prov.get_login(device_id)
    if not cfg:
        say("Provisioning API refused (see the terminal)")
        return False
    say("Writing the login onto the module (AT+CFTRANRX C:/mqtt.json)")
    if not prov.write_device_config(ser, cfg):
        say("Writing the login onto the module failed")
        return False
    dev.set_device(cfg)
    set_state(device=device_id)
    say(f"Module installed as {device_id}")
    return True


def connect(ser, dev):
    set_state(phase="connecting: network, clock, TLS, login")
    if dev.bring_up_network(ser) is None or not dev.check_clock(ser) or not dev.configure_ssl(ser):
        return False
    if not dev.connect_with_backoff(ser):
        return False
    dev.publish(ser, dev.TOPIC_STATUS, "online", qos=1)
    return True


def run(port=None):
    if not load_keys():
        return
    # After load_keys: provision_device reads MQTT_PROVISION_API_KEY when imported
    import serial
    import mqtt_tls_test as dev
    import provision_device as prov
    from dummy_data import dummy_reading
    from setup import send_at, reset_mqtt

    start_relay_watch()
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{HTTP_HOST}:{HTTP_PORT}"
    print(f"Install page: {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    ser = serial.Serial(port or dev.PORT, dev.BAUD, timeout=1)
    connected, seq = False, 0
    try:
        if "OK" not in send_at(ser, "AT"):
            say(f"Module not responding on {ser.port}. Check the USB cable and power.")
            set_state(phase="module not responding")
            while True:
                time.sleep(60)
        send_at(ser, "ATE0")
        set_state(imei=prov.read_imei(ser))
        cfg = dev.read_device_config(ser)
        if cfg:
            dev.set_device(cfg)
            set_state(device=dev.DEVICE_ID)
            say(f"Module already installed as {dev.DEVICE_ID}")
        else:
            set_state(phase="not installed: type a name and click Install")
            say("Module has no login yet: waiting for Install")

        while True:
            # A (new) install from the page
            try:
                device_id = requests.get(timeout=1 if cfg else 3600)
            except queue.Empty:
                device_id = None
            if device_id:
                if connected:
                    reset_mqtt(ser)
                    connected = False
                if install(ser, dev, prov, device_id):
                    cfg, seq = True, 0
                else:
                    set_state(phase="install failed: see the log, then try again")
                    continue
            if not cfg:
                continue

            if not connected:
                connected = connect(ser, dev)
                if not connected:
                    set_state(phase="could not connect: retrying in 60 s")
                    say("Could not connect (see the terminal); retrying in 60 s")
                    time.sleep(60)
                    continue
                set_state(phase=f"online: sending a reading every {EVERY} s")
                say(f"Connected as {dev.DEVICE_ID}; sending to {dev.DEVICE_ID}/up/data")

            seq += 1
            topic = f"{dev.DEVICE_ID}/up/data"
            payload = json.dumps(dummy_reading(seq), separators=(",", ":"))
            if dev.publish(ser, topic, payload, qos=1):
                remember("sent", {"at": datetime.datetime.now().strftime("%H:%M:%S"),
                                  "topic": topic, "payload": payload})
            else:
                say("Publish failed: reconnecting")
                connected = False
                continue
            # Wait for the next reading, but react to Install right away
            try:
                requests.put(requests.get(timeout=EVERY))
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        if connected:
            dev.publish(ser, dev.TOPIC_STATUS, "offline", qos=1)
        reset_mqtt(ser)
        ser.close()
        server.shutdown()
