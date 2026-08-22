#!/bin/sh
set -eu

java -jar /app/codeguard-gateway.jar &
gateway_pid=$!
python -m codeguard_agent.web_ui &
ui_pid=$!

cleanup() {
  kill "$gateway_pid" "$ui_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT
wait "$gateway_pid"
