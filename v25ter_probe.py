import serial
import time

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'
BAUD = 115200

# Chapter 2 of the A76XX manual (V.25TER basic command set), read/info-only
# subset. Deliberately excludes:
#   ATD, ATA, ATH, ATO, +++  - need/create an actual call or data session
#   AT&F                     - resets all settings to factory defaults
#   ATZ                      - reverts active config to the stored profile
#   AT&W                     - writes current settings to NVM
# ATV/ATQ/ATX are included bare (no argument) since the manual defines that
# as "set default value", which matches the state the module is already in.
PROBE_COMMANDS = [
    "ATI",
    "ATE0",
    "AT&V",
    "ATS0?",
    "ATV",
    "ATQ",
    "ATX",
    "AT+CGMI=?",
    "AT+CGMI",
    "AT+CGMM=?",
    "AT+CGMM",
    "AT+CGMR=?",
    "AT+CGMR",
    "AT+CGSN=?",
    "AT+CGSN",
    "AT+CSCS=?",
    "AT+CSCS?",
    "AT+GCAP=?",
    "AT+GCAP",
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

        results = {}
        for cmd in PROBE_COMMANDS:
            resp = send_at(ser, cmd)
            results[cmd] = "OK" if ("OK" in resp and "ERROR" not in resp) else "ERROR"
            print("-" * 40)

        print("\n=== V.25TER command support summary ===")
        for cmd, status in results.items():
            print(f"{status:6} {cmd}")
    finally:
        ser.close()


if __name__ == "__main__":
    run()
