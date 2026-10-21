import serial
import time
import datetime
import random
import json

# --- USER CONFIGURATION ---
PORT = '/dev/ttyUSB0'  # Change to your COM port (e.g., 'COM3' on Windows)
BAUD = 115200
SIM_PIN = "3759"       # Replace with your actual SIM PIN
APN_NAME = "safaricom"  # Replace with your carrier's APN (e.g., "safaricom")

# --- HTTP CONFIGURATION ---
HTTP_URL = "https://api.everyflow.org:10667"  # Updated server URL
HTTP_CONTENT_TYPE = "text/plain"

# --- SCHEDULED PAYLOADS (sent every 1 minute) ---
CUSTOM_PAYLOADS = [


    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",

]
def send_at(ser, command, wait_time=1, expect_response=True):
    print(f"Sending: {command}")
    ser.write((command + '\r\n').encode())
    time.sleep(wait_time)
    
    response = ""
    if expect_response:
        start_time = time.time()
        while time.time() - start_time < 5:
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

def send_http_post(ser, url, raw_payload):
    """Send an HTTP POST request with the raw hex payload."""
    print(f"Starting HTTP POST to {url} ({len(raw_payload)} bytes)...")
    
    # 1. Configure SSL for HTTPS (ssl_ctx_index=0)
    send_at(ser, 'AT+CSSLCFG="sslversion",0,4')       # TLS 1.2
    send_at(ser, 'AT+CSSLCFG="authmode",0,0')          # No cert verification
    send_at(ser, 'AT+CSSLCFG="enableSNI",0,1')         # Enable SNI
    send_at(ser, 'AT+CSSLCFG="ignorelocaltime",0,1')   # Ignore cert time check
    
    # 2. Terminate any previous HTTP session, then init fresh
    send_at(ser, "AT+HTTPTERM", expect_response=True)
    time.sleep(0.5)
    send_at(ser, "AT+HTTPINIT")
    
    # 3. Set HTTP parameters
    send_at(ser, f'AT+HTTPPARA="URL","{url}"')
    send_at(ser, f'AT+HTTPPARA="CONTENT","{HTTP_CONTENT_TYPE}"')
    send_at(ser, 'AT+HTTPPARA="SSLCFG",0')             # Link SSL context 0
    
    # 3. Enter data input mode
    # AT+HTTPDATA=<size>,<max_time>
    body_len = len(raw_payload)
    ser.write(f'AT+HTTPDATA={body_len},5000\r\n'.encode())
    
    # Wait for '>' or 'DOWNLOAD' prompt
    response = ""
    start = time.time()
    prompt_received = False
    while time.time() - start < 5:
        if ser.in_waiting > 0:
            response += ser.read(ser.in_waiting).decode(errors='ignore')
            if 'DOWNLOAD' in response or '>' in response:
                prompt_received = True
                break
        time.sleep(0.1)
    
    if prompt_received:
        ser.write(raw_payload.encode())
        time.sleep(1)
        # Wait for OK after data input
        wait_for_urc(ser, "OK", timeout=5)
    else:
        print("HTTP POST: Prompt not received")
        send_at(ser, "AT+HTTPTERM")
        return False

    # 4. Execute HTTP POST action (1 = POST)
    send_at(ser, "AT+HTTPACTION=1")
    
    # 5. Wait for HTTPACTION URC: +HTTPACTION: <method>,<status_code>,<datalen>
    urc = wait_for_urc(ser, "+HTTPACTION:", timeout=30)
    if urc:
        print(f"HTTP Action Result: {urc}")
        # Optionally read response
        if ",200," in urc:
            send_at(ser, "AT+HTTPREAD")
    else:
        print("HTTP POST: Action timeout")

    # 6. Terminate HTTP service
    send_at(ser, "AT+HTTPTERM")
    return True

def setup_4g_connection():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
        
        # Check if module is responding
        at_response = send_at(ser, "AT")
        if "OK" not in at_response:
            print("Module not responding. Check serial connection and power.")
            ser.close()
            return None
        
        # Disable echo
        send_at(ser, "ATE0")
        
        # 1. Initialize and Check SIM PIN
        cpin_check = send_at(ser, "AT+CPIN?")
        if "SIM PIN" in cpin_check:
            send_at(ser, f'AT+CPIN="{SIM_PIN}"')
            time.sleep(5)
        
        # 2. Set Phone Functionality to Full
        send_at(ser, "AT+CFUN=1")
        
        # Wait for URCs
        print("Waiting for +CPIN: READY and +CREG: 0,1...")
        wait_for_urc(ser, "+CPIN: READY", timeout=10)
        wait_for_urc(ser, "+CREG: 0,1", timeout=15)
        
        # 3. Check Network Registration
        reg_status = send_at(ser, "AT+CREG?")
        if not ("+CREG: 0,1" in reg_status or "+CREG: 0,5" in reg_status):
            print("Waiting for network registration...")
            time.sleep(10)
            send_at(ser, "AT+CREG?")
        
        # 4. Attach to GPRS
        send_at(ser, "AT+CGATT=1")
        
        # 5. Configure APN (PDP Context)
        send_at(ser, f'AT+CGDCONT=1,"IP","{APN_NAME}"')
        
        # 6. Activate PDP Context
        send_at(ser, "AT+CGACT=1,1")
        
        # 7. Get IP Address
        send_at(ser, "AT+CGPADDR=1")
        
        print("4G connection established.")
        
        try:
            SEND_INTERVAL = 60  # 1 minute between sends
            payload = CUSTOM_PAYLOADS[0]
            send_count = 0
            print(f"Sending payload via HTTP POST every {SEND_INTERVAL}s (Ctrl+C to stop)...")

            # Send the first one right away
            send_count += 1
            print(f"[#{send_count}] Sending HTTP POST...")
            send_http_post(ser, HTTP_URL, payload)
            last_send_time = time.time()

            while True:
                now_ts = time.time()

                if now_ts - last_send_time >= SEND_INTERVAL:
                    send_count += 1
                    print(f"[#{send_count}] Sending HTTP POST...")
                    send_http_post(ser, HTTP_URL, payload)
                    last_send_time = now_ts
                
                time.sleep(1)
        except KeyboardInterrupt:
            print("Exiting...")
        
        return ser
        
    except Exception as e:
        print(f"Error: {e}")
        if 'ser' in locals(): 
            ser.close()
        return None

if __name__ == "__main__":
    setup_4g_connection()
