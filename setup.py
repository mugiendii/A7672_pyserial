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

# --- MQTT CONFIGURATION ---
MQTT_BROKER_URI = "tcp://157.173.107.18:1883"
MQTT_CLIENT_ID = "A7672Client"
MQTT_TOPIC_PUB = "a7672/status"
MQTT_TOPIC_SUB = "a7672/#"
MQTT_USERNAME = "admin"
MQTT_PASSWORD = "mugiendii"

# --- SCHEDULED PAYLOADS (sent every 1 minute) ---
CUSTOM_PAYLOAD_TOPIC = "a7672/data"
CUSTOM_PAYLOADS = [
    "68250100103254769881239025012b140000002b50000000000000000004ddccbbaa502800005523270426200000b916",
    "68250100103254769881239025022b280000002b3c000000000000000004ddccbbaa2029000025002704262000003816",
    "68250100103254769881239025032b3c0000002b28000000000000000004ddccbbaa1529000055002704262000005e16",
    "68250100103254769881239025042b500000002b14000000000000000004ddccbbaa902800002501270426200000aa16",
    "68250100103254769881239025052b500000002b0000000000000000000744332211252900005501270426200000fc16",
    "68250100103254769881239025062b640000002b1e00000000000000000212903cfa3029000025022704262000003416",
    "68250100103254769881239025072b780000002b0a00000000000000000212903cfa1029000055022704262000004516",
    "68250100103254769881239025082b8c0000002b28000000000000000004ddccbbaa752800002503270426200000e516",
    "68250100103254769881239025092ba00000002b14000000000000000004ddccbbaa6028000055032704262000000116",
    "682501001032547698812390250a2ba00000002b00000000000000000007443322115028000025042704262000004e16",
    "682501001032547698812390250b2baa0000002b0000000000000000000212903cfa802800005504270426200000e216",
    "682501001032547698812390250c2bbe0000002b32000000000000000003887766550029000025052704262000005e16",
    "682501001032547698812390250d2bd20000002b1e000000000000000003887766550529000055052704262000009416",
    "682501001032547698812390250e2be60000002b0a00000000000000000388776655952800002506270426200000f516",
    "682501001032547698812390250f2bfa0000002b00000000000000000004ddccbbaa7028000055062704262000006016",
    "68250100103254769881239025102b0e0100002b1400000000000000000212903cfa6528000025072704262000001816",
    "68250100103254769881239025112b220100002b0000000000000000000212903cfa6028000055072704262000004416",
    "68250100103254769881239025122b360100002b2800000000000000000744332211102900002508270426200000da16",
    "68250100103254769881239025132b4a0100002b14000000000000000007443322112029000055082704262000001b16",
    "68250100103254769881239025142b5e0100002b0000000000000000000744332211152900002509270426200000e216",
]

# --- DATA TOPICS ---
DATA_TOPICS = {
    "a7672/data/temp_sensor": ["temp", "hum"],
}


def generate_dummy_data(topic):
    """Generate realistic dummy JSON payloads for each data topic."""
    now = datetime.datetime.now().isoformat()
    if topic == "a7672/data/temp_sensor":
        return json.dumps({
            "temp": round(random.uniform(18.0, 35.0), 2),
            "hum": round(random.uniform(30.0, 90.0), 2),
            "unit_temp": "°C",
            "unit_hum": "%",
            "timestamp": now
        })
    return json.dumps({"status": "unknown", "timestamp": now})


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


def reset_mqtt(ser):
    """Try to cleanly reset MQTT state"""
    print("Attempting MQTT state reset...")
    send_at(ser, "AT+CMQTTDISC=0", expect_response=False)
    time.sleep(0.5)
    send_at(ser, "AT+CMQTTREL=0", expect_response=False)
    time.sleep(0.5)
    send_at(ser, "AT+CMQTTSTOP", expect_response=False)
    time.sleep(1)


def setup_mqtt(ser, ip_address=None):
    print("Setting up MQTT connection with CMQTT...")

    # Step 3: CLEAN MQTT ENGINE - proper sequence
    print("Cleaning MQTT engine...")
    reset_mqtt(ser)

    # Step 4: Start MQTT
    print("Starting MQTT...")
    resp = send_at(ser, "AT+CMQTTSTART", wait_time=2)
    if "OK" not in resp:
        print(f"MQTT start command failed")
        return False
    # Check if URC is already in the response, or wait for it
    if "+CMQTTSTART: 0" not in resp:
        urc = wait_for_urc(ser, "+CMQTTSTART: 0", timeout=5)
        if not urc:
            print(f"MQTT start URC not received")
            return False
    print("MQTT engine started")

    # Step 5: Acquire client
    resp = send_at(ser, f'AT+CMQTTACCQ=0,"{MQTT_CLIENT_ID}"')
    if "OK" not in resp:
        print(f"MQTT client acquire failed")
        return False

    # Step 5b: Configure MQTT context (must be after ACCQ, before CONNECT)
    print("Configuring MQTT context...")
    resp = send_at(ser, 'AT+CMQTTCFG="version",0,4')  # MQTT 3.1.1
    if "OK" not in resp:
        print(f"MQTT version config failed")
        return False
    
    resp = send_at(ser, 'AT+CMQTTCFG="optimeout",0,60')  # 60 second timeout
    if "OK" not in resp:
        print(f"MQTT timeout config failed")
        return False

    # Step 6: Connect with authentication
    if MQTT_USERNAME and MQTT_PASSWORD:
        connect_cmd = f'AT+CMQTTCONNECT=0,"{MQTT_BROKER_URI}",60,1,"{MQTT_USERNAME}","{MQTT_PASSWORD}"'
    else:
        connect_cmd = f'AT+CMQTTCONNECT=0,"{MQTT_BROKER_URI}",60,1'
    resp = send_at(ser, connect_cmd, wait_time=3)
    if "OK" not in resp:
        print(f"MQTT connect command failed")
        return False
    # Check if URC is already in the response, or wait for it
    if "+CMQTTCONNECT: 0,0" not in resp:
        urc = wait_for_urc(ser, "+CMQTTCONNECT:", timeout=15)
        if not urc:
            print(f"MQTT connect URC not received (timeout)")
            return False
        if "+CMQTTCONNECT: 0,0" not in urc:
            print(f"MQTT connect failed with URC: {urc}")
            return False
    print("MQTT connected successfully!")

    # Step 7: Subscribe to command topic
    print(f"Subscribing to topic: {MQTT_TOPIC_SUB}")
    topic_sub = MQTT_TOPIC_SUB
    ser.write(f'AT+CMQTTSUB=0,{len(topic_sub)},1\r\n'.encode())
    time.sleep(0.5)

    # Wait for '>' prompt then send topic
    response = ""
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting).decode(errors='ignore')
            response += data
            if '>' in response:
                ser.write(topic_sub.encode())
                time.sleep(0.5)
                break
        time.sleep(0.1)

    # Wait for subscribe result URC
    sub_urc = wait_for_urc(ser, "+CMQTTSUB:", timeout=10)
    if sub_urc and "+CMQTTSUB: 0,0" in sub_urc:
        print(f"Subscribed to {MQTT_TOPIC_SUB}")
    else:
        print(f"Subscribe result: {sub_urc}")

    # Step 8: Publish a test message
    print("Publishing test message...")
    topic = MQTT_TOPIC_PUB
    payload = "Device connected"
    
    # Set topic - this returns '>' prompt
    print(f"Setting topic: {topic}")
    ser.write(f'AT+CMQTTTOPIC=0,{len(topic)}\r\n'.encode())
    time.sleep(0.5)
    
    # Read until we get the '>' prompt
    response = ""
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting).decode(errors='ignore')
            response += data
            print(f"Topic response: {response}")
            if '>' in response:
                ser.write(topic.encode())
                time.sleep(0.5)
                break
        time.sleep(0.1)
    
    # Set payload - this also returns '>' prompt
    print(f"Setting payload: {payload}")
    ser.write(f'AT+CMQTTPAYLOAD=0,{len(payload)}\r\n'.encode())
    time.sleep(0.5)
    
    # Read until we get the '>' prompt
    response = ""
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting).decode(errors='ignore')
            response += data
            print(f"Payload response: {response}")
            if '>' in response:
                ser.write(payload.encode())
                time.sleep(0.5)
                break
        time.sleep(0.1)
    
    # Publish - QoS=1, timeout=60
    print("Publishing...")
    resp = send_at(ser, 'AT+CMQTTPUB=0,1,60')
    print(f"Publish result: {resp}")
    
    print("MQTT ready for messages")
    return True


def publish_message(ser, topic, payload):
    """Publish a single MQTT message. Returns True on success."""
    # Set topic
    ser.write(f'AT+CMQTTTOPIC=0,{len(topic)}\r\n'.encode())
    time.sleep(0.5)
    response = ""
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting > 0:
            response += ser.read(ser.in_waiting).decode(errors='ignore')
            if '>' in response:
                ser.write(topic.encode())
                time.sleep(0.5)
                break
        time.sleep(0.1)
    else:
        print("Publish: topic prompt timeout")
        return False

    # Set payload
    ser.write(f'AT+CMQTTPAYLOAD=0,{len(payload)}\r\n'.encode())
    time.sleep(0.5)
    response = ""
    start = time.time()
    while time.time() - start < 3:
        if ser.in_waiting > 0:
            response += ser.read(ser.in_waiting).decode(errors='ignore')
            if '>' in response:
                ser.write(payload.encode())
                time.sleep(0.5)
                break
        time.sleep(0.1)
    else:
        print("Publish: payload prompt timeout")
        return False

    # Publish QoS 1
    resp = send_at(ser, 'AT+CMQTTPUB=0,1,60')
    if 'OK' in resp:
        print(f"Published: {payload}")
        return True
    else:
        print(f"Publish failed: {resp}")
        return False


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
            time.sleep(5)  # Wait for SIM to initialize
        
        # 2. Set Phone Functionality to Full
        send_at(ser, "AT+CFUN=1")
        
        # Wait for URCs
        print("Waiting for +CPIN: READY and +CREG: 0,1...")
        urc_cpin = wait_for_urc(ser, "+CPIN: READY", timeout=10)
        urc_creg = wait_for_urc(ser, "+CREG: 0,1", timeout=10)
        if not urc_cpin:
            print("CPIN READY not received")
        if not urc_creg:
            print("CREG 0,1 not received")
        
        # 3. Check Network Registration
        reg_status = send_at(ser, "AT+CREG?")
        if not ("+CREG: 0,1" in reg_status or "+CREG: 0,5" in reg_status):
            print("Waiting for network registration...")
            time.sleep(10)
            reg_status = send_at(ser, "AT+CREG?")
        
        # 4. Attach to GPRS
        send_at(ser, "AT+CGATT=1")
        
        # 5. Configure APN (PDP Context)
        send_at(ser, f'AT+CGDCONT=1,"IP","{APN_NAME}"')
        
        # 6. Activate PDP Context
        send_at(ser, "AT+CGACT=1,1")
        
        # 7. Get IP Address
        ip = None
        ip_response = send_at(ser, "AT+CGPADDR=1")
        if "+CGPADDR: 1," in ip_response:
            parts = ip_response.split(',')
            if len(parts) >= 2:
                ip = parts[1].strip()
                print(f"IP Address: {ip}")
            else:
                print("Failed to parse IP address")
        else:
            print("Failed to get IP address")
        
        # 8. Setup SMS monitoring
        send_at(ser, "AT+CMGF=1")          # Set to Text Mode 
        send_at(ser, "AT+CNMI=2,1,0,0,0")  # Set New Message Indications 
        
        # Verify PDP is active before MQTT
        time.sleep(1)
        pdp_check = send_at(ser, "AT+CGACT?")
        if "+CGACT: 1,1" not in pdp_check:
            print("Warning: PDP context may not be active")

        if ip:
            setup_mqtt(ser, ip)

        print("4G connection established. IP: " + (ip if ip else "Unknown"))
        print("Monitoring for MQTT messages and new SMS messages... (Press Ctrl+C to exit)")
        
        try:
            SEND_INTERVAL = 60  # 1 minute between each payload
            payload_index = 0
            last_send_time = time.time()  # send first one immediately
            total_payloads = len(CUSTOM_PAYLOADS)
            print(f"Will send {total_payloads} payloads to {CUSTOM_PAYLOAD_TOPIC}, one per minute.")

            # Send the first payload right away
            print(f"[1/{total_payloads}] Sending payload #{payload_index + 1}...")
            publish_message(ser, CUSTOM_PAYLOAD_TOPIC, CUSTOM_PAYLOADS[payload_index])
            payload_index += 1

            while True:
                now_ts = time.time()

                # Send next payload every 60 seconds until all are done
                if payload_index < total_payloads and now_ts - last_send_time >= SEND_INTERVAL:
                    print(f"[{payload_index + 1}/{total_payloads}] Sending payload #{payload_index + 1}...")
                    publish_message(ser, CUSTOM_PAYLOAD_TOPIC, CUSTOM_PAYLOADS[payload_index])
                    payload_index += 1
                    last_send_time = now_ts

                if payload_index >= total_payloads and now_ts - last_send_time >= SEND_INTERVAL:
                    print("All payloads sent. Continuing to monitor...")
                    last_send_time = now_ts  # reset to suppress repeated prints

                if ser.in_waiting > 0:
                    line = ser.readline().decode(errors='ignore').strip()
                    if "+CMTI:" in line:
                        # New SMS received
                        parts = line.split(',')
                        if len(parts) >= 2:
                            index = parts[1].strip('"')
                            print(f"New SMS at index {index}")
                            # Read the SMS
                            sms_response = send_at(ser, f"AT+CMGR={index}")
                            # Parse SMS content if needed
                    elif "+CMQTTRXSTART:" in line:
                        # Incoming MQTT message notification
                        print(f"MQTT message incoming: {line}")
                        # Read the full message payload
                        msg_data = ""
                        msg_start = time.time()
                        while time.time() - msg_start < 5:
                            if ser.in_waiting > 0:
                                msg_data += ser.read(ser.in_waiting).decode(errors='ignore')
                                if "+CMQTTRXEND:" in msg_data:
                                    break
                            time.sleep(0.1)
                        print(f"MQTT message data: {msg_data.strip()}")
                    elif "+CMQTTSUB:" in line:
                        print(f"MQTT subscribe status: {line}")
                    elif "+CMQTTPUB:" in line:
                        print(f"MQTT publish status: {line}")
                    elif "+CMQTTCONNLOST:" in line:
                        print(f"MQTT connection lost: {line}")
                time.sleep(0.5)
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