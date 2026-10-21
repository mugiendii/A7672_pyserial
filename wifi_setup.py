import serial
import time

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'  # Change to your COM port (e.g., 'COM3' on Windows)
BAUD = 115200
SIM_PIN = "3242"        # Replace with your actual SIM PIN
APN_NAME = "safaricom"  # Replace with your carrier's APN

# --- WIFI SCAN CONFIGURATION ---
# NOTE: wifi_probe.py confirmed this module only implements the WiFi *scan*
# commands (AT+CWSTASCAN/EX/SYN). The hotspot/AP commands (AT+CWMAP,
# AT+CWSSID, AT+CWAUTH, AT+CWMOCH, AT+CWISO, AT+CWMACADDR, AT+CWCLICNT) all
# return ERROR on this firmware, so the module can neither host its own
# WiFi network nor join one — it can only scan for nearby networks.
SCAN_INTERVAL = 30  # seconds between WiFi scans


def send_at(ser, command, wait_time=1, expect_response=True, timeout=5):
    print(f"Sending: {command}")
    ser.write((command + '\r\n').encode())
    time.sleep(wait_time)

    response = ""
    if expect_response:
        start_time = time.time()
        while time.time() - start_time < timeout:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting).decode(errors='ignore')
                response += data
                if '\nOK\r\n' in response or '\nERROR\r\n' in response:
                    time.sleep(0.1)
                    if ser.in_waiting > 0:
                        response += ser.read(ser.in_waiting).decode(errors='ignore')
                    break
            time.sleep(0.1)

    print(f"Response: {response.strip()}")
    return response.strip()


def wait_for_urc(ser, prefix, timeout=15):
    response = ""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting).decode(errors='ignore')
            response += data
            if prefix in response:
                return response.strip()
        time.sleep(0.1)
    return None


def setup_4g(ser):
    """Bring up the cellular data link: SIM PIN -> registration -> APN -> PDP context."""
    at_response = send_at(ser, "AT")
    if "OK" not in at_response:
        print("Module not responding. Check serial connection and power.")
        return False

    send_at(ser, "ATE0")  # Disable echo

    # SIM PIN
    cpin_check = send_at(ser, "AT+CPIN?")
    if "SIM PIN" in cpin_check:
        send_at(ser, f'AT+CPIN="{SIM_PIN}"')
        time.sleep(5)

    # Full functionality
    send_at(ser, "AT+CFUN=1")

    print("Waiting for +CPIN: READY and +CREG: 0,1...")
    wait_for_urc(ser, "+CPIN: READY", timeout=10)
    wait_for_urc(ser, "+CREG: 0,1", timeout=15)

    reg_status = send_at(ser, "AT+CREG?")
    if not ("+CREG: 0,1" in reg_status or "+CREG: 0,5" in reg_status):
        print("Waiting for network registration...")
        time.sleep(10)
        reg_status = send_at(ser, "AT+CREG?")
        if not ("+CREG: 0,1" in reg_status or "+CREG: 0,5" in reg_status):
            print("Network registration failed.")
            return False

    # Attach to GPRS and configure/activate PDP context
    send_at(ser, "AT+CGATT=1")
    send_at(ser, f'AT+CGDCONT=1,"IP","{APN_NAME}"')
    send_at(ser, "AT+CGACT=1,1")

    ip_response = send_at(ser, "AT+CGPADDR=1")
    if "+CGPADDR: 1," in ip_response:
        ip = ip_response.split(',', 1)[1].strip()
        print(f"4G connected. IP Address: {ip}")
    else:
        print("Warning: could not confirm IP address; PDP context may not be active.")

    return True


def parse_cwstascan(response):
    """Parse '+CWSTASCAN: <bssid>,<channel>,<signal>' lines into a list of dicts."""
    networks = []
    for line in response.splitlines():
        line = line.strip()
        if not line or line.startswith('+CWSTASCAN:') and ',' not in line:
            continue
        line = line.replace('+CWSTASCAN:', '').strip()
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 3 and ':' in parts[0]:
            bssid, channel, signal = parts
            networks.append({'bssid': bssid, 'channel': channel, 'signal': signal})
    return networks


def scan_wifi(ser):
    """Run a WiFi scan and return a list of {bssid, channel, signal} dicts."""
    resp = send_at(ser, "AT+CWSTASCAN", wait_time=3, timeout=15)
    if "OK" not in resp:
        print("WiFi scan failed.")
        return []
    networks = parse_cwstascan(resp)
    print(f"Found {len(networks)} nearby WiFi network(s):")
    for net in networks:
        print(f"  {net['bssid']}  channel={net['channel']}  signal={net['signal']}dBm")
    return networks


def monitor_wifi(ser):
    """Periodically scan for nearby WiFi networks until interrupted."""
    print(f"Scanning for WiFi networks every {SCAN_INTERVAL}s... (Press Ctrl+C to exit)")
    try:
        while True:
            scan_wifi(ser)
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        print("Exiting...")


def run():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"Could not open {PORT}: {e}")
        return

    try:
        if not setup_4g(ser):
            print("4G setup failed. Aborting WiFi scan.")
            return

        monitor_wifi(ser)
    finally:
        ser.close()
        print("Serial port closed.")


if __name__ == "__main__":
    run()
