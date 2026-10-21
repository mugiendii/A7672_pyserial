#include <WiFi.h>

// WiFi credentials
const char* ssid = "guest";
const char* password = "grundfosguest08";

// TCP Server details
const char* host = "api.everyflow.org";
const uint16_t port = 10667;

// Function to convert hex string to byte array
void hexToBytes(const char* hex, uint8_t* bytes, size_t length) {
for (size_t i = 0; i < length; i++) {
sscanf(hex + 2 * i, "%2hhx", &bytes[i]);
}
}

void setup() {
Serial.begin(115200);
delay(1000);

// Connect to WiFi
Serial.println();
Serial.print("Connecting to ");
Serial.println(ssid);

WiFi.begin(ssid, password);

while (WiFi.status() != WL_CONNECTED) {
delay(500);
Serial.print(".");
}

Serial.println("");
Serial.println("WiFi connected");
Serial.print("IP address: ");
Serial.println(WiFi.localIP());

// Example: Send a "heartbeat" payload on startup
// Presets from your original code:
// heartbeat: "fefe6810AAAAAAAAAAAAAA8000A216"
// open_valve: "fefe6810AAAAAAAAAAAAAA0404A01700553216"
// close_valve: "fefe6810AAAAAAAAAAAAAA0404A01700997616"
sendTCPData("fefe6810AAAAAAAAAAAAAA8000A216");
}

void loop() {
// You can add logic here to send data periodically or based on triggers
delay(10000);
}

void sendTCPData(const char* hexPayload) {
WiFiClient client;

Serial.print("Connecting to ");
Serial.print(host);
Serial.print(":");
Serial.println(port);

if (!client.connect(host, port)) {
Serial.println("Connection failed");
return;
}

size_t payloadLen = strlen(hexPayload) / 2;
uint8_t bytes[payloadLen];
hexToBytes(hexPayload, bytes, payloadLen);

Serial.print("Sending payload: ");
Serial.println(hexPayload);
client.write(bytes, payloadLen);

// Wait for response (5 second timeout)
unsigned long timeout = millis();
while (client.available() == 0) {
if (millis() - timeout > 5000) {
Serial.println(">>> Client Timeout!");
client.stop();
return;
}
}

// Read and print response in Hex
Serial.print("Response (HEX): ");
while (client.available()) {
uint8_t b = client.read();
if (b < 16) Serial.print("0");
Serial.print(b, HEX);
}
Serial.println();

Serial.println("Closing connection");
client.stop();
}