/**
 * a7672_tcp_client.cpp
 *
 * SIM7672 AT-command TCP client for Arduino / ESP32 / STM32.
 * Mirrors tcp_setup.py exactly — same AT sequence, same frame format.
 *
 * Frame layout:
 *   FE FE | 68 10 | <7-byte serial REVERSED> | 80 00 | CS | 16
 *   CS = sum(68 10 <reversed serial> 80 00) mod 256
 *
 * ── Wiring ────────────────────────────────────────────────────────────────────
 *  MCU            MODEM_TX pin   MODEM_RX pin   Serial object
 *  ESP32          GPIO17         GPIO16         Serial2
 *  Arduino Mega   TX1 (18)       RX1 (19)       Serial1
 *  Arduino Uno    use SoftwareSerial (change MODEM_SERIAL below)
 */

#include <Arduino.h>

// ── Hardware selection ────────────────────────────────────────────────────────
// Change MODEM_SERIAL to match your wiring (Serial1, Serial2, etc.)
#define MODEM_SERIAL    Serial1
#define DEBUG_SERIAL    Serial

// ── User configuration ────────────────────────────────────────────────────────
#define MODEM_BAUD      115200
#define DEBUG_BAUD      115200

#define SIM_PIN         "3759"          // set "" if SIM has no PIN
#define APN             "safaricom"

#define TCP_HOST        "api.everyflow.org"
#define TCP_PORT        10667
#define LINK_NUM        0

// 14 hex chars (7 bytes) — actual meter serial number
// Frame stores it byte-reversed; build_heartbeat() handles that automatically
#define METER_SERIAL    "98765432100001"

#define SEND_INTERVAL_MS  60000UL       // milliseconds between transmissions

// ── Internal state ────────────────────────────────────────────────────────────
static uint8_t  s_frame[15];
static uint8_t  s_frameLen = 0;
static uint32_t s_lastSend = 0;
static uint32_t s_txCount  = 0;

// ── Low-level serial helpers ──────────────────────────────────────────────────

/**
 * Send an AT command and return the full response string.
 * Waits up to timeoutMs for OK or ERROR.
 */
String sendAT(const char* cmd, uint32_t timeoutMs = 3000) {
    while (MODEM_SERIAL.available()) MODEM_SERIAL.read();  // drain stale bytes

    DEBUG_SERIAL.print("  >> ");
    DEBUG_SERIAL.println(cmd);
    MODEM_SERIAL.print(cmd);
    MODEM_SERIAL.print("\r\n");

    String resp = "";
    uint32_t start = millis();
    while (millis() - start < timeoutMs) {
        while (MODEM_SERIAL.available())
            resp += (char)MODEM_SERIAL.read();
        if (resp.indexOf("\nOK\r\n")    >= 0 ||
            resp.indexOf("\nERROR\r\n") >= 0)
            break;
        delay(20);
    }
    resp.trim();
    DEBUG_SERIAL.print("  << ");
    DEBUG_SERIAL.println(resp);
    return resp;
}

/**
 * Block until a URC containing `prefix` arrives, or until timeoutMs elapses.
 * Returns the full accumulated buffer, or "" on timeout.
 */
String waitForURC(const char* prefix, uint32_t timeoutMs = 15000) {
    String buf = "";
    uint32_t start = millis();
    while (millis() - start < timeoutMs) {
        while (MODEM_SERIAL.available())
            buf += (char)MODEM_SERIAL.read();
        if (buf.indexOf(prefix) >= 0)
            return buf;
        delay(50);
    }
    return "";
}

/** Discard any pending bytes from the modem. */
void drain(uint32_t settleMs = 300) {
    delay(settleMs);
    while (MODEM_SERIAL.available()) MODEM_SERIAL.read();
}

// ── Frame builder ─────────────────────────────────────────────────────────────

static uint8_t hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return 0;
}

/**
 * Build a 15-byte heartbeat frame into `out`.
 * serialHex must be exactly 14 hex characters (7 bytes).
 * Returns the frame length (always 15).
 */
uint8_t buildHeartbeat(const char* serialHex, uint8_t* out) {
    // Decode serial bytes
    uint8_t serial[7];
    for (int i = 0; i < 7; i++)
        serial[i] = (hexNibble(serialHex[i * 2]) << 4) |
                     hexNibble(serialHex[i * 2 + 1]);

    // Body: 68 10 + reversed serial + 80 00
    uint8_t body[11];
    body[0] = 0x68;
    body[1] = 0x10;
    for (int i = 0; i < 7; i++)
        body[2 + i] = serial[6 - i];   // byte-reverse
    body[9]  = 0x80;
    body[10] = 0x00;

    // Checksum = sum(body) mod 256
    uint8_t cs = 0;
    for (int i = 0; i < 11; i++) cs += body[i];

    // Full frame: FE FE + body + CS + 16
    out[0] = 0xFE;
    out[1] = 0xFE;
    memcpy(out + 2, body, 11);
    out[13] = cs;
    out[14] = 0x16;
    return 15;
}

// ── Network setup ─────────────────────────────────────────────────────────────

bool setupNetwork() {
    if (sendAT("AT").indexOf("OK") < 0) {
        DEBUG_SERIAL.println("[NET] Module not responding.");
        return false;
    }
    sendAT("ATE0");

    // SIM PIN
    String cpin = sendAT("AT+CPIN?");
    if (cpin.indexOf("SIM PIN") >= 0) {
        DEBUG_SERIAL.println("[NET] Entering SIM PIN...");
        sendAT("AT+CPIN=\"" SIM_PIN "\"");
        delay(5000);
    }

    sendAT("AT+CFUN=1");

    DEBUG_SERIAL.println("[NET] Waiting for SIM ready and registration...");
    waitForURC("+CPIN: READY", 10000);
    waitForURC("+CREG: 0,1",   20000);

    String reg = sendAT("AT+CREG?");
    if (reg.indexOf("+CREG: 0,1") < 0 && reg.indexOf("+CREG: 0,5") < 0) {
        DEBUG_SERIAL.println("[NET] Waiting extra time for registration...");
        delay(10000);
        sendAT("AT+CREG?");
    }

    sendAT("AT+CGATT=1");
    sendAT("AT+CGDCONT=1,\"IP\",\"" APN "\"");

    DEBUG_SERIAL.println("[NET] Starting socket service (AT+NETOPEN)...");
    String resp = sendAT("AT+NETOPEN", 10000);
    if (resp.indexOf("+NETOPEN: 0") < 0 && resp.indexOf("OK") < 0) {
        String state = sendAT("AT+NETOPEN?");
        if (state.indexOf("+NETOPEN: 1") < 0) {
            DEBUG_SERIAL.println("[NET] NETOPEN failed.");
            return false;
        }
    }

    String ip = sendAT("AT+IPADDR");
    DEBUG_SERIAL.print("[NET] IP address: ");
    DEBUG_SERIAL.println(ip);

    // Manual receive buffering — prevents server response mixing with AT replies
    sendAT("AT+CIPRXGET=1");
    return true;
}

// ── TCP helpers ───────────────────────────────────────────────────────────────

bool tcpOpen() {
    char cmd[80];
    snprintf(cmd, sizeof(cmd),
             "AT+CIPOPEN=%d,\"TCP\",\"%s\",%d", LINK_NUM, TCP_HOST, TCP_PORT);
    DEBUG_SERIAL.print("\n[TCP] Connecting to ");
    DEBUG_SERIAL.print(TCP_HOST); DEBUG_SERIAL.print(":");
    DEBUG_SERIAL.println(TCP_PORT);

    String resp = sendAT(cmd, 10000);
    String successURC = "+CIPOPEN: " + String(LINK_NUM) + ",0";

    if (resp.indexOf(successURC) >= 0) {
        DEBUG_SERIAL.println("[TCP] Socket opened.");
        return true;
    }

    DEBUG_SERIAL.println("[TCP] Waiting for CIPOPEN URC...");
    String urc = waitForURC(("+CIPOPEN: " + String(LINK_NUM) + ",").c_str(), 15000);
    if (urc.length() > 0 && urc.indexOf(successURC) >= 0) {
        DEBUG_SERIAL.println("[TCP] Socket opened (deferred).");
        return true;
    }

    DEBUG_SERIAL.print("[TCP] Failed to open socket: ");
    DEBUG_SERIAL.println(urc);
    return false;
}

void tcpClose() {
    DEBUG_SERIAL.println("[TCP] Closing socket...");
    char cmd[32];
    snprintf(cmd, sizeof(cmd), "AT+CIPCLOSE=%d", LINK_NUM);
    String resp = sendAT(cmd, 10000);

    String ok4   = "+CIPCLOSE: " + String(LINK_NUM) + ",0";
    String close4 = "+CIPCLOSE: " + String(LINK_NUM) + ",4";  // already closed by remote — OK
    if (resp.indexOf(ok4) < 0 && resp.indexOf(close4) < 0)
        waitForURC(("+CIPCLOSE: " + String(LINK_NUM) + ",").c_str(), 5000);

    drain();
}

bool tcpSend(const uint8_t* data, uint8_t len) {
    DEBUG_SERIAL.print("[TCP] Sending "); DEBUG_SERIAL.print(len);
    DEBUG_SERIAL.print(" bytes: ");
    for (int i = 0; i < len; i++) {
        if (data[i] < 0x10) DEBUG_SERIAL.print("0");
        DEBUG_SERIAL.print(data[i], HEX);
    }
    DEBUG_SERIAL.println();

    // Issue CIPSEND in fixed-length mode
    char cmd[32];
    snprintf(cmd, sizeof(cmd), "AT+CIPSEND=%d,%d", LINK_NUM, len);
    DEBUG_SERIAL.print("  >> "); DEBUG_SERIAL.println(cmd);
    MODEM_SERIAL.print(cmd);
    MODEM_SERIAL.print("\r\n");

    // Wait for '>' prompt
    String promptBuf = "";
    uint32_t start = millis();
    while (millis() - start < 5000) {
        while (MODEM_SERIAL.available())
            promptBuf += (char)MODEM_SERIAL.read();
        if (promptBuf.indexOf('>') >= 0) break;
        delay(10);
    }
    if (promptBuf.indexOf('>') < 0) {
        DEBUG_SERIAL.println("[TCP] No '>' prompt received.");
        return false;
    }

    // Send raw binary frame
    MODEM_SERIAL.write(data, len);

    // Wait for +CIPSEND confirmation URC
    String result = "";
    start = millis();
    while (millis() - start < 10000) {
        while (MODEM_SERIAL.available())
            result += (char)MODEM_SERIAL.read();
        if (result.indexOf("+CIPSEND:") >= 0) break;
        delay(50);
    }
    DEBUG_SERIAL.print("  << "); DEBUG_SERIAL.println(result);

    // Verify reqLen == cnfLen
    String expected = "+CIPSEND: " + String(LINK_NUM) + "," + String(len) + "," + String(len);
    if (result.indexOf(expected) >= 0) {
        DEBUG_SERIAL.println("[TCP] Send confirmed.");
        return true;
    }
    DEBUG_SERIAL.println("[TCP] Send confirmation mismatch.");
    return false;
}

String tcpReadResponse(uint32_t timeoutMs = 10000) {
    DEBUG_SERIAL.print("[TCP] Waiting up to ");
    DEBUG_SERIAL.print(timeoutMs / 1000);
    DEBUG_SERIAL.println("s for server response...");

    String dataURC  = "+CIPRXGET: 1," + String(LINK_NUM);
    String closeURC = "+IPCLOSE: "    + String(LINK_NUM) + ",";
    String buf = "";
    uint32_t start = millis();

    while (millis() - start < timeoutMs) {
        while (MODEM_SERIAL.available())
            buf += (char)MODEM_SERIAL.read();

        // Data buffered and ready
        if (buf.indexOf(dataURC) >= 0) {
            DEBUG_SERIAL.println("[TCP] Data notification — fetching HEX...");
            drain();
            char readCmd[32];
            snprintf(readCmd, sizeof(readCmd), "AT+CIPRXGET=3,%d", LINK_NUM);
            String rx = sendAT(readCmd, 5000);
            DEBUG_SERIAL.print("[TCP] Response: "); DEBUG_SERIAL.println(rx);
            return rx;
        }

        // Server closed connection — try to read any buffered data
        if (buf.indexOf(closeURC) >= 0) {
            DEBUG_SERIAL.println("[TCP] Server closed connection — checking buffer...");
            delay(200);
            drain();
            char readCmd[32];
            snprintf(readCmd, sizeof(readCmd), "AT+CIPRXGET=3,%d", LINK_NUM);
            String rx = sendAT(readCmd, 5000);
            if (rx.indexOf("+CIPRXGET:") >= 0 && rx.indexOf("No data") < 0) {
                DEBUG_SERIAL.print("[TCP] Response: "); DEBUG_SERIAL.println(rx);
                return rx;
            }
            DEBUG_SERIAL.println("[TCP] No data buffered after server close.");
            return "";
        }

        delay(50);
    }

    DEBUG_SERIAL.println("[TCP] No response within timeout.");
    return "";
}

// ── Arduino entry points ──────────────────────────────────────────────────────

void setup() {
    DEBUG_SERIAL.begin(DEBUG_BAUD);
    MODEM_SERIAL.begin(MODEM_BAUD);
    delay(2000);

    // Build heartbeat frame once at startup
    s_frameLen = buildHeartbeat(METER_SERIAL, s_frame);

    DEBUG_SERIAL.println("================================================");
    DEBUG_SERIAL.println("A7672 TCP Client");
    DEBUG_SERIAL.print  ("Target : " TCP_HOST ":");
    DEBUG_SERIAL.println(TCP_PORT);
    DEBUG_SERIAL.print  ("Serial : " METER_SERIAL " → frame: ");
    for (int i = 0; i < s_frameLen; i++) {
        if (s_frame[i] < 0x10) DEBUG_SERIAL.print("0");
        DEBUG_SERIAL.print(s_frame[i], HEX);
    }
    DEBUG_SERIAL.println();
    DEBUG_SERIAL.println("================================================");

    if (!setupNetwork()) {
        DEBUG_SERIAL.println("[MAIN] Network setup failed. Halting.");
        while (true) delay(1000);
    }

    // Trigger first transmission immediately
    s_lastSend = millis() - SEND_INTERVAL_MS;
}

void loop() {
    if (millis() - s_lastSend >= SEND_INTERVAL_MS) {
        s_lastSend = millis();
        s_txCount++;

        DEBUG_SERIAL.println("\n========================================");
        DEBUG_SERIAL.print("[MAIN] Transmission #");
        DEBUG_SERIAL.println(s_txCount);

        if (!tcpOpen()) {
            DEBUG_SERIAL.println("[MAIN] Socket open failed — skipping cycle.");
            return;
        }

        if (tcpSend(s_frame, s_frameLen)) {
            tcpReadResponse();
        } else {
            DEBUG_SERIAL.println("[MAIN] Send failed.");
        }

        tcpClose();
    }
}
