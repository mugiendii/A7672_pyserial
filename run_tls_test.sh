#!/usr/bin/env bash
# Runs the local relay and the A7672 TLS test together; sends one command when the device starts listening.
# The modem uses the login stored by provision_device.py; MQTT_DEVICE_ID only picks what the relay watches.
#   MQTT_DEVICE_ID=oceo2 MQTT_RELAY_DESKTOP_PASSWORD=... ./run_tls_test.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python

: "${MQTT_DEVICE_ID:?set MQTT_DEVICE_ID}"
: "${MQTT_RELAY_DESKTOP_PASSWORD:?set MQTT_RELAY_DESKTOP_PASSWORD}"
export MQTT_CLIENT_ID="${MQTT_CLIENT_ID:-$MQTT_DEVICE_ID-test}"   # don't kick off the live device
CMD="${CMD:-{\"action\":\"read\"}}"

$PY -u mqtt_relay_local.py --device "$MQTT_DEVICE_ID" 2>&1 | sed -u 's/^/[relay] /' &
RELAY_PID=$!
trap 'kill $RELAY_PID 2>/dev/null || true' EXIT
sleep 3

$PY -u mqtt_tls_test.py 2>&1 | while IFS= read -r line; do
    echo "[modem] $line"
    if [[ "$line" == Listening\ on* ]]; then
        $PY mqtt_relay_local.py --device "$MQTT_DEVICE_ID" --cmd "$CMD" --once 2>&1 | sed 's/^/[cmd]   /' &
    fi
done

sleep 3   # let the relay print the final offline status
