import serial
import time
import re

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'  # Change to your COM port (e.g., 'COM3' on Windows)
BAUD = 115200

# How often to poll AT+CGNSSINFO while waiting for a fix (seconds)
POLL_INTERVAL = 3


def send_at(ser, command, wait_time=1, expect_response=True):
    print(f"Sending: {command}")
    ser.write((command + '\r\n').encode())
    time.sleep(wait_time)

    response = ""
    if expect_response:
        start_time = time.time()
        while time.time() - start_time < 5:  # Wait up to 5 seconds for response
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting).decode(errors='ignore')
                response += data
                if '\nOK\r\n' in response or '\nERROR\r\n' in response:
                    # Some URCs arrive immediately after the final OK/ERROR.
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


def power_on_gnss(ser):
    """Make sure the GNSS engine is powered on (AT+CGNSSPWR=1)."""
    status = send_at(ser, "AT+CGNSSPWR?")
    if re.search(r'\+CGNSSPWR:\s*1', status):
        print("GNSS already powered on.")
        return True

    resp = send_at(ser, "AT+CGNSSPWR=1", wait_time=2)
    if "OK" not in resp:
        print("Failed to power on GNSS.")
        return False

    if "+CGNSSPWR: READY!" not in resp:
        print("Waiting for GNSS engine to become ready...")
        urc = wait_for_urc(ser, "+CGNSSPWR: READY!", timeout=12)
        if not urc:
            print("Timed out waiting for '+CGNSSPWR: READY!'.")
            return False

    print("GNSS engine ready.")
    return True


def parse_cgnssinfo(response):
    """
    Parse a +CGNSSINFO response.

    Format:
    +CGNSSINFO: <mode>,<GPS-SVs>,<GLONASS-SVs>,<BEIDOU-SVs>,<lat>,<N/S>,<lon>,
                <E/W>,<date>,<UTC-time>,<alt>,<speed>,<course>,<PDOP>,<HDOP>,<VDOP>

    Returns a dict with parsed fields, or None if the line wasn't found.
    A missing/empty <lat> means there is no fix yet.
    """
    match = re.search(r'\+CGNSSINFO:\s*(.*)', response)
    if not match:
        return None

    fields = [f.strip() for f in match.group(1).split(',')]
    # Pad to expected length so indexing never raises
    fields += [''] * (16 - len(fields))
    mode, gps_svs, glonass_svs, beidou_svs, lat, ns, lon, ew, date, utc, \
        alt, speed, course, pdop, hdop, vdop = fields[:16]

    result = {
        'mode': mode,
        'gps_svs': gps_svs,
        'glonass_svs': glonass_svs,
        'beidou_svs': beidou_svs,
        'fix': bool(lat and lon),
        'lat': None,
        'lon': None,
        'alt': alt,
        'speed': speed,
        'course': course,
        'hdop': hdop,
    }

    if result['fix']:
        lat_val = float(lat)
        lon_val = float(lon)
        if ns.upper() == 'S':
            lat_val = -lat_val
        if ew.upper() == 'W':
            lon_val = -lon_val
        result['lat'] = lat_val
        result['lon'] = lon_val

    return result


def get_location(ser, max_wait=120):
    """Poll AT+CGNSSINFO until a fix is obtained or max_wait seconds elapse."""
    start = time.time()
    while time.time() - start < max_wait:
        resp = send_at(ser, "AT+CGNSSINFO", wait_time=1)
        info = parse_cgnssinfo(resp)

        if info is None:
            print("Could not parse +CGNSSINFO response.")
        elif info['fix']:
            print("\n--- GNSS Fix Acquired ---")
            print(f"Latitude:  {info['lat']}")
            print(f"Longitude: {info['lon']}")
            print(f"Altitude:  {info['alt']} m")
            print(f"Satellites: GPS={info['gps_svs']} "
                  f"GLONASS={info['glonass_svs']} BEIDOU={info['beidou_svs']}")
            print(f"HDOP: {info['hdop']}")
            return info['lat'], info['lon']
        else:
            print(f"No fix yet (GPS={info['gps_svs']} "
                  f"GLONASS={info['glonass_svs']} BEIDOU={info['beidou_svs']} "
                  f"visible). Make sure the antenna has a clear view of the sky.")

        time.sleep(POLL_INTERVAL)

    print("Timed out waiting for a GNSS fix.")
    return None


def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"Could not open {PORT}: {e}")
        return

    try:
        at_response = send_at(ser, "AT")
        if "OK" not in at_response:
            print("Module not responding. Check serial connection and power.")
            return

        send_at(ser, "ATE0")  # Disable echo

        if not power_on_gnss(ser):
            return

        get_location(ser, max_wait=120)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
