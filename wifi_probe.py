import serial
import time

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'
BAUD = 115200

# Every AT+CW* command from the A76XX manual's WiFi chapter, tested with
# their test/read form so nothing here mutates the module's state.
PROBE_COMMANDS = [
    "AT+CWSTASCAN=?",
    "AT+CWSTASCAN?",
    "AT+CWSTASCANEX=?",
    "AT+CWSTASCANEX?",
    "AT+CWSTASCANSYN=?",
    "AT+CWMAP=?",
    "AT+CWMAP?",
    "AT+CWSSID?",
    "AT+CWAUTH?",
    "AT+CWMOCH?",
    "AT+CWISO=?",
    "AT+CWISO?",
    "AT+CWMACADDR=?",
    "AT+CWCLICNT?",
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
        for cmd in PROBE_COMMANDS:
            resp = send_at(ser, cmd)
            results[cmd] = "OK" if ("OK" in resp and "ERROR" not in resp) else "ERROR"
            print("-" * 40)

        print("\n=== WiFi command support summary ===")
        for cmd, status in results.items():
            print(f"{status:6} {cmd}")
    finally:
        ser.close()


if __name__ == "__main__":
    run()
