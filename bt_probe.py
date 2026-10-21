import serial
import time

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'
BAUD = 115200

# The manual requires AT+BLEPOWER=1 / AT+BTPOWER=1 before most other BLE/BT
# commands will respond with anything other than ERROR, so those two writes
# are included even though the rest of this script is read-only otherwise.
BLE_PROBE_COMMANDS = [
    "AT+BLEPOWER=?",
    "AT+BLEPOWER=1",
    "AT+BLEPOWER?",
    "AT+BLEHOST?",
    "AT+BLEADDR?",
    "AT+BLESTATUS=?",
    "AT+BLESTATUS?",
    "AT+BLESCAN=?",
    "AT+BLESCAN?",
]

BT_PROBE_COMMANDS = [
    "AT+BTPOWER=?",
    "AT+BTPOWER=1",
    "AT+BTPOWER?",
    "AT+BTHOST?",
    "AT+BTADDR?",
    "AT+BTIOCAP?",
    "AT+BTSCAN=?",
    "AT+BTPAIRED?",
    "AT+BTSPPSRV=?",
    "AT+BTSPPPROF=?",
]


def send_at(ser, command, wait_time=1, timeout=5):
    print(f"Sending: {command}")
    ser.write((command + '\r\n').encode())
    time.sleep(wait_time)

    response = ""
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


def probe_group(ser, label, commands, results):
    print(f"\n--- {label} ---")
    for cmd in commands:
        resp = send_at(ser, cmd, wait_time=2 if 'POWER=1' in cmd else 1)
        results[cmd] = "OK" if ("OK" in resp and "ERROR" not in resp) else "ERROR"
        print("-" * 40)


def run():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"Could not open {PORT}: {e}")
        return

    try:
        if "OK" not in send_at(ser, "AT"):
            print("Module not responding. Check serial connection and power.")
            return
        send_at(ser, "ATE0")

        results = {}
        probe_group(ser, "BLE (chapter 26.2)", BLE_PROBE_COMMANDS, results)
        probe_group(ser, "Classic BT (chapter 26.3)", BT_PROBE_COMMANDS, results)

        print("\n=== Bluetooth command support summary ===")
        for cmd, status in results.items():
            print(f"{status:6} {cmd}")
    finally:
        ser.close()


if __name__ == "__main__":
    run()
