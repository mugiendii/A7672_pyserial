import serial
import time

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'   # Change to your COM port (e.g., 'COM3' on Windows)
BAUD = 115200
SIM_PIN = "3759"        # Replace with your actual SIM PIN
APN_NAME = "safaricom"  # Replace with your carrier's APN

# --- TCP SERVER CONFIGURATION ---
TCP_HOST = "api.everyflow.org"
TCP_PORT = 10667
LINK_NUM = 0            # Socket index 0-9

# --- PAYLOAD ---
# Server log confirmed frame layout (after stripping fefe):
#   68  10  [68][serial_6bytes]  80 00  [CS]  16
#       ^    ^   ^               ^
#       |    |   6-byte serial   control+len
#       |    frame start byte embedded in address field
#       second header byte
#
# Server extracts serial = 6 bytes from the 7-byte address field (drops leading 68)
# DB query: SELECT ... WHERE serial_number = '<serial_6bytes_hex>'
#
# METER_SERIAL = 14 hex chars (7 bytes) — the actual serial number of the meter.
# The serial is stored REVERSED in the frame; 6810 is a fixed header, not serial.
# Frame: FE FE | 68 10 | <serial reversed> | 80 00 | CS | 16
#
# All-AA test  : "aaaaaaaaaaaaaa"  (reversed = same, used for connectivity test)
# Real device  : serial from meter label or JSON "address" field e.g. "68753600035015"
METER_SERIAL = "98765432100001"

def build_heartbeat(serial_hex):
    """Build a heartbeat frame for the given 14-hex-char (7-byte) serial.

    The serial bytes are reversed before placing them in the frame.
    Frame: FE FE | 68 10 | <serial reversed> | 80 00 | CS | 16
    CS = sum(68 10 <reversed serial> 80 00) mod 256
    """
    reversed_serial = bytes.fromhex(serial_hex)[::-1]
    body = bytes.fromhex("6810") + reversed_serial + bytes.fromhex("8000")
    cs   = sum(body) % 256
    return "fefe" + body.hex() + f"{cs:02x}" + "16"

PAYLOAD_HEX = build_heartbeat(METER_SERIAL)

# --- SEND MODE ---
# True  = send payload as ASCII hex text  (e.g. "FEFE6810..." as ASCII chars)
# False = send payload as binary bytes    (confirmed correct by Go reference code)
SEND_AS_ASCII = False

# --- SCHEDULE ---
SEND_INTERVAL = 60      # seconds between sends


# ---------------------------------------------------------------------------
# Low-level serial helpers
# ---------------------------------------------------------------------------

def send_at(ser, command, wait_time=1, expect_response=True, timeout=5):
    """Send an AT command and return the full response string."""
    print(f"  >> {command}")
    ser.write((command + '\r\n').encode())
    time.sleep(wait_time)

    response = ""
    if expect_response:
        start = time.time()
        while time.time() - start < timeout:
            if ser.in_waiting > 0:
                chunk = ser.read(ser.in_waiting).decode(errors='ignore')
                response += chunk
                if '\nOK\r\n' in response or '\nERROR\r\n' in response:
                    time.sleep(0.1)
                    if ser.in_waiting > 0:
                        response += ser.read(ser.in_waiting).decode(errors='ignore')
                    break
            time.sleep(0.1)

    print(f"  << {response.strip()}")
    return response.strip()


def wait_for_urc(ser, prefix, timeout=15):
    """Block until a URC containing `prefix` arrives, or timeout."""
    response = ""
    start = time.time()
    while time.time() - start < timeout:
        if ser.in_waiting > 0:
            chunk = ser.read(ser.in_waiting).decode(errors='ignore')
            response += chunk
            if prefix in response:
                return response.strip()
        time.sleep(0.1)
    return None


def drain(ser, settle=0.3):
    """Read and discard any pending bytes."""
    time.sleep(settle)
    if ser.in_waiting > 0:
        ser.read(ser.in_waiting)


# ---------------------------------------------------------------------------
# TCP send/receive
# ---------------------------------------------------------------------------

def tcp_send_hex(ser, link_num, hex_payload):
    """
    Send data over an open TCP socket.

    SEND_AS_ASCII=True  → sends the hex digits as ASCII text (e.g. "FEFE68...")
    SEND_AS_ASCII=False → decodes hex and sends raw binary bytes

    Uses AT+CIPSEND=<link_num>,<length> (fixed-length mode).
    The module responds with '>' then accepts exactly <length> bytes.

    Returns True on success, False on failure.
    """
    if SEND_AS_ASCII:
        raw_bytes = hex_payload.upper().encode('ascii')
        mode_tag  = "ASCII hex"
    else:
        raw_bytes = bytes.fromhex(hex_payload)
        mode_tag  = "binary"
    length = len(raw_bytes)

    print(f"\n[TCP] Sending {length} bytes ({mode_tag}): {hex_payload.upper()}")

    # Issue the CIPSEND command
    cmd = f'AT+CIPSEND={link_num},{length}'
    print(f"  >> {cmd}")
    ser.write((cmd + '\r\n').encode())

    # Wait for the '>' data prompt
    prompt_buf = ""
    start = time.time()
    got_prompt = False
    while time.time() - start < 5:
        if ser.in_waiting > 0:
            prompt_buf += ser.read(ser.in_waiting).decode(errors='ignore')
            if '>' in prompt_buf:
                got_prompt = True
                break
        time.sleep(0.05)

    if not got_prompt:
        print("[TCP] ERROR: no '>' prompt received from CIPSEND")
        return False

    # Write the raw binary payload (no encoding, no line ending)
    ser.write(raw_bytes)

    # Wait for OK + +CIPSEND URC
    result_buf = ""
    start = time.time()
    while time.time() - start < 10:
        if ser.in_waiting > 0:
            result_buf += ser.read(ser.in_waiting).decode(errors='ignore')
            if '+CIPSEND:' in result_buf:
                break
        time.sleep(0.1)

    print(f"  << {result_buf.strip()}")

    # Sanity-check: reqSendLength == cnfSendLength
    # URC format: +CIPSEND: <link_num>,<reqLen>,<cnfLen>
    if '+CIPSEND:' in result_buf:
        try:
            urc_line = [l for l in result_buf.splitlines() if '+CIPSEND:' in l][0]
            parts = urc_line.split(':')[1].split(',')
            req, cnf = int(parts[1].strip()), int(parts[2].strip())
            if req == cnf:
                print(f"[TCP] Send confirmed: {cnf}/{req} bytes delivered to stack")
                return True
            else:
                print(f"[TCP] WARNING: sent {req} but only {cnf} confirmed")
                return False
        except Exception:
            pass  # fall through

    return 'OK' in result_buf


def tcp_read_response(ser, link_num, timeout=10):
    """
    Read incoming data on the socket using CIPRXGET mode 1 (manual buffering).

    setup_network() enables mode 1 so incoming bytes are held in the module
    buffer and are never mixed with AT command responses.  Two URCs matter:

      +CIPRXGET: 1,<link_num>   — data is waiting; poll with AT+CIPRXGET=3
      +IPCLOSE:  <link_num>,<r> — server closed the connection

    On either URC we fetch whatever is buffered and return the hex string.
    Returns an empty string when nothing arrives within <timeout> seconds.
    """
    print(f"[TCP] Waiting up to {timeout}s for server response...")
    buf = ""
    start = time.time()

    while time.time() - start < timeout:
        if ser.in_waiting > 0:
            buf += ser.read(ser.in_waiting).decode(errors='ignore')

        # Data buffered and ready to read
        if f'+CIPRXGET: 1,{link_num}' in buf:
            print("[TCP] Data notification — fetching HEX...")
            drain(ser)
            rx = send_at(ser, f'AT+CIPRXGET=3,{link_num}', wait_time=0.5, timeout=5)
            print(f"[TCP] Response:\n{rx}")
            return rx

        # Server closed the connection — try to read any buffered data first
        if f'+IPCLOSE: {link_num},' in buf:
            close_info = [l for l in buf.splitlines() if f'+IPCLOSE: {link_num},' in l]
            print(f"[TCP] Server closed connection: {close_info}")
            time.sleep(0.2)
            drain(ser)
            rx = send_at(ser, f'AT+CIPRXGET=3,{link_num}', wait_time=0.5, timeout=5)
            if '+CIPRXGET:' in rx and 'No data' not in rx:
                print(f"[TCP] Response:\n{rx}")
                return rx
            print("[TCP] No data buffered after server close.")
            return ""

        time.sleep(0.1)

    print("[TCP] No response received within timeout.")
    return ""


# ---------------------------------------------------------------------------
# TCP connection lifecycle
# ---------------------------------------------------------------------------

def tcp_open(ser, link_num, host, port):
    """Open a TCP socket. Returns True on success.

    AT+CIPOPEN returns OK immediately, then fires the async URC
    '+CIPOPEN: <n>,<err>' up to ~120 s later.  Because send_at() stops
    reading as soon as it sees 'OK', the URC often arrives *after*
    send_at returns — so we capture it in a dedicated wait_for_urc call
    that reads whatever is still coming in on the wire.
    """
    success_urc = f'+CIPOPEN: {link_num},0'
    fail_prefix  = f'+CIPOPEN: {link_num},'

    print(f"\n[TCP] Opening connection to {host}:{port} (link {link_num})...")

    # Send command; give the module 2 s to emit both OK and the URC if fast
    immediate = send_at(ser, f'AT+CIPOPEN={link_num},"TCP","{host}",{port}',
                        wait_time=2, timeout=10)

    # Best case: URC already in the same read window
    if success_urc in immediate:
        print(f"[TCP] Socket {link_num} opened successfully (immediate).")
        return True

    # URC arrived but shows an error code
    if fail_prefix in immediate and success_urc not in immediate:
        print(f"[TCP] CIPOPEN error in immediate response: {immediate}")
        return False

    # URC not yet seen — wait for it on the wire (up to 15 s)
    print("[TCP] URC not yet received, waiting...")
    urc = wait_for_urc(ser, fail_prefix, timeout=15)
    if urc and success_urc in urc:
        print(f"[TCP] Socket {link_num} opened successfully (deferred URC).")
        return True

    print(f"[TCP] Failed to open socket. URC: {urc!r}  immediate: {immediate!r}")
    return False


def tcp_close(ser, link_num):
    """Close the TCP socket and wait for the +CIPCLOSE URC.

    Error code 4 means the remote already closed the connection — treat as OK.
    """
    print(f"\n[TCP] Closing socket {link_num}...")
    resp = send_at(ser, f'AT+CIPCLOSE={link_num}', wait_time=2, timeout=10)
    if f'+CIPCLOSE: {link_num},0' not in resp and f'+CIPCLOSE: {link_num},4' not in resp:
        wait_for_urc(ser, f'+CIPCLOSE: {link_num},', timeout=5)
    drain(ser)  # flush stale bytes before next open


# ---------------------------------------------------------------------------
# Network bring-up
# ---------------------------------------------------------------------------

def setup_network(ser):
    """
    Bring up the cellular data link: SIM PIN → registration →
    PDP context → AT+NETOPEN (activates the TCP/IP stack).

    Returns True when the network is ready, False on failure.
    """
    # Basic comms check
    if "OK" not in send_at(ser, "AT"):
        print("[NET] Module not responding. Check serial connection.")
        return False

    send_at(ser, "ATE0")  # disable echo

    # SIM PIN
    cpin = send_at(ser, "AT+CPIN?")
    if "SIM PIN" in cpin:
        print("[NET] Entering SIM PIN...")
        send_at(ser, f'AT+CPIN="{SIM_PIN}"')
        time.sleep(5)

    # Full functionality
    send_at(ser, "AT+CFUN=1")

    # Wait for registration URCs
    print("[NET] Waiting for SIM ready and network registration...")
    wait_for_urc(ser, "+CPIN: READY", timeout=10)
    wait_for_urc(ser, "+CREG: 0,1",   timeout=20)

    reg = send_at(ser, "AT+CREG?")
    if "+CREG: 0,1" not in reg and "+CREG: 0,5" not in reg:
        print("[NET] Waiting extra time for network registration...")
        time.sleep(10)
        send_at(ser, "AT+CREG?")

    # PDP context
    send_at(ser, "AT+CGATT=1")
    send_at(ser, f'AT+CGDCONT=1,"IP","{APN_NAME}"')

    # Start TCP/IP stack — this is the A76XX equivalent of "bring up data"
    # AT+NETOPEN activates the PDP context and starts the socket service.
    print("[NET] Starting socket service (AT+NETOPEN)...")
    resp = send_at(ser, "AT+NETOPEN", wait_time=2, timeout=10)
    if "+NETOPEN: 0" not in resp and "OK" not in resp:
        # Already open is fine — check current state
        state = send_at(ser, "AT+NETOPEN?")
        if "+NETOPEN: 1" not in state:
            print("[NET] AT+NETOPEN failed.")
            return False

    ip = send_at(ser, "AT+IPADDR")
    print(f"[NET] IP address: {ip}")

    # Enable manual receive buffering so server responses are never mixed
    # with AT command replies (avoids race condition in mode 0 auto-output)
    send_at(ser, "AT+CIPRXGET=1")

    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print(f"[SERIAL] Cannot open {PORT}: {e}")
        return

    try:
        print("=" * 60)
        print("A76XX TCP Socket Client")
        print(f"Target : {TCP_HOST}:{TCP_PORT}")
        print(f"Payload: {PAYLOAD_HEX.upper()}")
        print(f"Interval: {SEND_INTERVAL}s")
        print("=" * 60)

        if not setup_network(ser):
            print("[MAIN] Network setup failed. Aborting.")
            return

        send_count = 0

        def do_send():
            nonlocal send_count
            send_count += 1
            print(f"\n{'='*40}")
            print(f"[MAIN] Transmission #{send_count}")

            if not tcp_open(ser, LINK_NUM, TCP_HOST, TCP_PORT):
                print("[MAIN] Could not open socket — skipping this cycle.")
                return

            if tcp_send_hex(ser, LINK_NUM, PAYLOAD_HEX):
                tcp_read_response(ser, LINK_NUM, timeout=10)
            else:
                print("[MAIN] Send failed.")

            tcp_close(ser, LINK_NUM)

        # First send immediately
        do_send()
        last_send = time.time()

        while True:
            if time.time() - last_send >= SEND_INTERVAL:
                do_send()
                last_send = time.time()
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[MAIN] Interrupted by user.")
    finally:
        # Best-effort cleanup
        try:
            tcp_close(ser, LINK_NUM)
            send_at(ser, "AT+NETCLOSE", wait_time=1)
        except Exception:
            pass
        ser.close()
        print("[MAIN] Serial port closed.")


if __name__ == "__main__":
    run()