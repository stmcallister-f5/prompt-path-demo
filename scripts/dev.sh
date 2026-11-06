#!/usr/bin/env bash
# Run the whole stack on a laptop, no cluster needed.
# The devrouter plays the part of the slot Services; the board uses the local driver.
set -euo pipefail
cd "$(dirname "$0")/../app"
H=127.0.0.1
R=http://$H:9000
export PYTHONPATH="$PWD"
COMPONENTS="{\"waf\":\"http://$H:9101\",\"aigw\":\"http://$H:9102\",\"guard\":\"http://$H:9103\",\"agent\":\"http://$H:9104\",\"guard-tools\":\"http://$H:9106\",\"tools\":\"http://$H:9107\"}"
SLOTS='{"slot-waf":"waf","slot-aigw":"aigw","slot-guard":"guard","slot-tools":"guard-tools"}'

pids=()
start() { env HOST=$H "$@" python3 -m demo.main & pids+=($!); }
trap 'kill ${pids[@]} 2>/dev/null' EXIT

start ROLE=devrouter PORT=9000 DEV_COMPONENTS="$COMPONENTS" DEV_SLOTS="$SLOTS"
start ROLE=waf        PORT=9101 COMPONENT=waf  DISPLAY_NAME="Edge WAF"   UPSTREAM=$R/slot/slot-aigw
start ROLE=aigw       PORT=9102 COMPONENT=aigw DISPLAY_NAME="AI gateway" UPSTREAM=$R/slot/slot-guard
start ROLE=guardrail  PORT=9103 COMPONENT=guard DISPLAY_NAME="Guardrail" GUARDRAIL_MODE=inbound UPSTREAM=http://$H:9104
start ROLE=agent      PORT=9104 COMPONENT=agent DISPLAY_NAME="Agent" MODEL_URL=http://$H:9105 TOOLS_URL=$R/slot/slot-tools MODEL_DISPLAY="Model"
start ROLE=model      PORT=9105 COMPONENT=model
start ROLE=guardrail  PORT=9106 COMPONENT=guard-tools DISPLAY_NAME="Guardrail (tools)" GUARDRAIL_MODE=tools UPSTREAM=http://$H:9107
start ROLE=tools      PORT=9107 COMPONENT=tools
start ROLE=board      PORT=${BOARD_PORT:-9100} BOARD_CONFIG="$PWD/../deploy/overlays/no-gateway/board-config.yaml" BOARD_DRIVER=local DEVROUTER_URL=$R GATEWAY_URL=$R/slot/slot-waf BOARD_SETTLE_SECONDS=0

echo "Toggle board: http://$H:${BOARD_PORT:-9100}"
wait
