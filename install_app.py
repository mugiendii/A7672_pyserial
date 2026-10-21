"""Device install page (zero-touch, broker repo PROVISIONING.md).

    MQTT_RELAY_DESKTOP_PASSWORD=... MQTT_PROVISION_API_KEY=... .venv/bin/python install_app.py
    then open http://127.0.0.1:8080

1. A new device boots without a login and claims itself: it shows up under "New devices" by IMEI.
2. The installer types its name (id = username = client id) and clicks Approve. The provisioning API
   creates the login and sends it to that device only; this app never sees the password.
3. The device reconnects with its login and sends data: it turns online. Installed.
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

import mqtt_relay_local as relay

HTTP_HOST = "127.0.0.1"   # Local only: the page can create device logins
HTTP_PORT = 8080
PROVISION_API = os.environ.get("MQTT_PROVISION_API_URL", "https://mqtt-api.nodewave.dev").rstrip("/")
# Can create and delete every device login: keep it in the environment, never in the page
PROVISION_API_KEY = os.environ.get("MQTT_PROVISION_API_KEY", "")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
IMEI_RE = re.compile(r"^[0-9]{15}$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def provision_api(method, path, body=None):
    """Call the provisioning API. Returns the JSON body (None for 204). Raises ApiError."""
    req = urllib.request.Request(
        PROVISION_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {PROVISION_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", "")
        except ValueError:
            message = ""
        raise ApiError(e.code, message or f"provisioning API: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(502, f"provisioning API unreachable: {getattr(e, 'reason', e)}")


def approve(imei, device_id):
    result = provision_api("POST", f"/v1/claims/{imei}/approve", {"id": device_id})
    # Approved: the device gets its login and reconnects; it is installed once its data arrives
    relay.register_device(device_id, state="pending", imei=imei, error=None,
                          approved_at=datetime.datetime.now().isoformat(timespec="seconds"))
    print(f"Approved IMEI {imei} as {device_id}; waiting for its data")
    return result


def devices_view():
    now = datetime.datetime.now()
    with relay.registry_lock:
        registry = relay.load_registry()
    out = []
    for device_id, e in sorted(registry.items()):
        state = e["state"]
        last = e.get("last_seen")
        if state == "online" and last and now - datetime.datetime.fromisoformat(last) > relay.SILENT_AFTER:
            state = "silent"
        out.append({"id": device_id, "state": state, "imei": e.get("imei"),
                    "installed_at": e.get("installed_at"), "last_seen": last})
    return out


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):
        try:
            if self.path == "/":
                self.send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/devices":
                self.send(200, devices_view())
            elif self.path == "/api/claims":
                claims = provision_api("GET", "/v1/claims")["claims"]
                self.send(200, [c for c in claims if c["status"] == "pending"])
            else:
                self.send(404, {"error": "not found"})
        except ApiError as e:
            self.send(e.status, {"error": e.message})

    def do_POST(self):
        body = self.body()
        imei, device_id = str(body.get("imei", "")), str(body.get("id", "")).strip()
        if not IMEI_RE.match(imei):
            return self.send(400, {"error": "bad IMEI"})
        try:
            if self.path == "/api/approve":
                if not ID_RE.match(device_id):
                    return self.send(400, {"error": "Name: lowercase letters, digits and -, 2-31 characters"})
                approve(imei, device_id)
                self.send(200, {"id": device_id})
            elif self.path == "/api/reject":
                provision_api("DELETE", f"/v1/claims/{imei}")
                self.send(200, {"imei": imei})
            else:
                self.send(404, {"error": "not found"})
        except ApiError as e:
            self.send(e.status, {"error": e.message})

    def log_message(self, fmt, *args):
        pass   # Keep the terminal for device messages


def start_mqtt():
    """Listen to every device's data; the relay marks approved devices online on their first data."""
    userdata = {"device": None, "cmd": None, "install": None}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{relay.MQTT_CLIENT_ID}-install",
                         userdata=userdata)
    client.username_pw_set(relay.MQTT_USERNAME, relay.MQTT_PASSWORD)
    client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.on_connect = relay.on_connect
    client.on_subscribe = relay.on_subscribe
    client.on_disconnect = relay.on_disconnect
    client.on_message = relay.on_message
    client.connect_async(relay.MQTT_HOST, relay.MQTT_PORT, keepalive=relay.KEEPALIVE)
    client.loop_start()
    return client


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Device Install</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#6b7280; --border:#e3e6ea;
        --accent:#2563eb; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111418; --card:#1a1e24; --text:#e6e8eb; --muted:#9aa3ad; --border:#2b3138;
          --accent:#5b8def; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, sans-serif; }
main { max-width:900px; margin:0 auto; padding:24px 16px; }
h1 { font-size:20px; margin:0 0 16px; }
h2 { font-size:15px; margin:0 0 4px; }
.hint { color:var(--muted); font-size:13px; margin:0 0 12px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:16px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--border); vertical-align:middle; }
th { color:var(--muted); font-weight:500; font-size:13px; }
.mono { font-family:ui-monospace, monospace; }
input { font:inherit; padding:6px 8px; border:1px solid var(--border); border-radius:6px;
        background:var(--bg); color:var(--text); width:140px; }
button { font:inherit; padding:6px 14px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; }
button.ghost { background:none; color:var(--bad); padding:6px 8px; }
button:disabled { opacity:.5; cursor:default; }
.actions { display:flex; gap:6px; align-items:center; white-space:nowrap; }
.state { font-weight:600; }
.online { color:var(--ok); } .pending, .silent { color:var(--warn); } .offline { color:var(--bad); }
#msg { font-size:13px; min-height:1.5em; margin-bottom:8px; }
.err { color:var(--bad); }
</style>
</head>
<body>
<main>
  <h1>Device install</h1>
  <div id="msg"></div>
  <div class="card">
    <h2>New devices</h2>
    <p class="hint">Power on a device: it appears here by IMEI (printed on the modem label). Give it a name and approve it.</p>
    <table>
      <thead><tr><th>IMEI</th><th>Firmware</th><th>Last seen</th><th>Name</th></tr></thead>
      <tbody id="claims"><tr><td colspan="4">Loading...</td></tr></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Devices</h2>
    <table>
      <thead><tr><th>Name</th><th>State</th><th>IMEI</th><th>Installed</th><th>Last data</th></tr></thead>
      <tbody id="devices"></tbody>
    </table>
  </div>
</main>
<script>
const LABEL = { pending: "approved, waiting for data", online: "online", silent: "silent (no data 15 min)",
                offline: "offline" };
const esc = s => String(s ?? "-").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const time = s => s ? new Date(s).toLocaleString() : "-";
const msg = (text, bad) => { const m = document.getElementById("msg"); m.textContent = text; m.className = bad ? "err" : ""; };
const typed = {};   // keep names being typed across refreshes

async function call(path, body) {
  const r = await fetch(path, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  const out = await r.json();
  if (!r.ok) throw new Error(out.error);
  return out;
}

async function refreshClaims() {
  const tbody = document.getElementById("claims");
  document.querySelectorAll("#claims input").forEach(i => typed[i.dataset.imei] = i.value);
  if (document.activeElement && document.activeElement.closest("#claims")) return;   // don't redraw while typing
  const r = await fetch("/api/claims");
  const list = await r.json();
  if (!r.ok) { tbody.innerHTML = `<tr><td colspan="4" class="err">${esc(list.error)}</td></tr>`; return; }
  tbody.innerHTML = list.length ? list.map(c => `
    <tr><td class="mono">${esc(c.imei)}</td><td>${esc(c.fw)}</td><td>${time(c.last_seen)}</td>
        <td><form class="actions" data-imei="${esc(c.imei)}">
          <input data-imei="${esc(c.imei)}" placeholder="dev01" value="${esc(typed[c.imei] || "")}" autocomplete="off" required>
          <button>Approve</button><button type="button" class="ghost" data-reject>Reject</button>
        </form></td></tr>`).join("")
    : '<tr><td colspan="4">No new devices. Power one on; it claims within a minute.</td></tr>';
}

async function refreshDevices() {
  const list = await (await fetch("/api/devices")).json();
  document.getElementById("devices").innerHTML = list.length ? list.map(d => `
    <tr><td>${esc(d.id)}</td><td><span class="state ${d.state}">${esc(LABEL[d.state] || d.state)}</span></td>
        <td class="mono">${esc(d.imei)}</td><td>${time(d.installed_at)}</td><td>${time(d.last_seen)}</td></tr>`).join("")
    : '<tr><td colspan="5">No devices installed yet</td></tr>';
}

document.getElementById("claims").addEventListener("submit", async ev => {
  ev.preventDefault();
  const form = ev.target, imei = form.dataset.imei, id = form.querySelector("input").value.trim();
  form.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    await call("/api/approve", { imei, id });
    delete typed[imei];
    msg(`${id} approved. The device gets its login and reconnects; it turns online when its data arrives.`);
    document.activeElement.blur();
  } catch (e) { msg(e.message, true); form.querySelectorAll("button").forEach(b => b.disabled = false); }
  refresh();
});

document.getElementById("claims").addEventListener("click", async ev => {
  if (!ev.target.matches("[data-reject]")) return;
  const imei = ev.target.closest("form").dataset.imei;
  if (!confirm(`Reject IMEI ${imei}? Only for devices you don't recognise.`)) return;
  try { await call("/api/reject", { imei }); msg(`IMEI ${imei} rejected.`); } catch (e) { msg(e.message, true); }
  document.activeElement.blur();
  refresh();
});

function refresh() { refreshClaims().catch(() => {}); refreshDevices().catch(() => {}); }
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def main():
    if not relay.MQTT_PASSWORD or not PROVISION_API_KEY:
        print("Set MQTT_RELAY_DESKTOP_PASSWORD and MQTT_PROVISION_API_KEY (Infisical, Nodewave / prod / /mqtt).")
        sys.exit(1)
    client = start_mqtt()
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    print(f"Install page: http://{HTTP_HOST}:{HTTP_PORT}  (provisioning API {PROVISION_API})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
