#include <iostream>
#include <string>
#include <vector>
#include <sstream>
#include <iomanip>
#include <cstring>
#include <sys/socket.h>
#include <netdb.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <map>

// Helper to convert hex string to bytes
std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::vector<uint8_t> bytes;
    for (size_t i = 0; i < hex.length(); i += 2) {
        std::string byteString = hex.substr(i, 2);
        uint8_t byte = (uint8_t) strtol(byteString.c_str(), NULL, 16);
        bytes.push_back(byte);
    }
    return bytes;
}

// Helper to convert bytes to hex string
std::string bytes_to_hex(const uint8_t* data, size_t len) {
    std::stringstream ss;
    ss << std::hex << std::setfill('0') << std::uppercase;
    for (size_t i = 0; i < len; ++i) {
        ss << std::setw(2) << (int)data[i];
    }
    return ss.str();
}

int main(int argc, char* argv[]) {
    std::string address = "api.everyflow.org:10667";
    std::string payload_hex = "fefe6810AAAAAAAAAAAAAA8000A216";

    std::map<std::string, std::string> presets = {
        {"heartbeat", "fefe6810AAAAAAAAAAAAAA8000A216"},
        {"open_valve", "fefe6810AAAAAAAAAAAAAA0404A01700553216"},
        {"close_valve", "fefe6810AAAAAAAAAAAAAA0404A01700997616"},
        {"reset", "fefe6810AAAAAAAAAAAAAA21030A00004C16"}
    };

    // Simple argument parsing
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "-a" && i + 1 < argc) address = argv[++i];
        else if (arg == "-p" && i + 1 < argc) payload_hex = argv[++i];
        else if (arg == "-h" || arg == "--help") {
            std::cout << "Usage: " << argv[0] << " [-a address] [-p payload/preset]\n";
            std::cout << "Default address: api.everyflow.org:10667\n";
            std::cout << "Presets: heartbeat, open_valve, close_valve, reset\n";
            return 0;
        }
    }

    if (presets.count(payload_hex)) {
        payload_hex = presets[payload_hex];
    }

    // Split address into host and port
    size_t colon_pos = address.find(':');
    if (colon_pos == std::string::npos) {
        std::cerr << "Invalid address format. Use host:port\n";
        return 1;
    }
    std::string host = address.substr(0, colon_pos);
    std::string port = address.substr(colon_pos + 1);

    // Resolve DNS
    struct addrinfo hints, *res;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    if (getaddrinfo(host.c_str(), port.c_str(), &hints, &res) != 0) {
        std::cerr << "Could not resolve host: " << host << std::endl;
        return 1;
    }

    // Create socket
    int sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (sock == -1) {
        std::cerr << "Could not create socket\n";
        freeaddrinfo(res);
        return 1;
    }

    // Set timeout
    struct timeval tv;
    tv.tv_sec = 10;
    tv.tv_usec = 0;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof tv);

    // Connect
    std::cout << "Connecting to " << host << ":" << port << "..." << std::endl;
    if (connect(sock, res->ai_addr, res->ai_addrlen) == -1) {
        std::cerr << "Connection failed\n";
        close(sock);
        freeaddrinfo(res);
        return 1;
    }
    freeaddrinfo(res);

    // Send payload
    std::vector<uint8_t> data = hex_to_bytes(payload_hex);
    std::cout << "Sending payload (" << data.size() << " bytes): " << payload_hex << std::endl;
    if (send(sock, data.data(), data.size(), 0) == -1) {
        std::cerr << "Send failed\n";
        close(sock);
        return 1;
    }

    // Receive response
    uint8_t buffer[4096];
    std::cout << "Waiting for response..." << std::endl;
    ssize_t n = recv(sock, buffer, sizeof(buffer), 0);
    if (n > 0) {
        std::cout << "Received response (" << n << " bytes):" << std::endl;
        std::cout << "HEX: " << bytes_to_hex(buffer, n) << std::endl;
    } else if (n == 0) {
        std::cout << "Connection closed by server\n";
    } else {
        std::cerr << "Receive failed or timed out\n";
    }

    close(sock);
    return 0;
}
