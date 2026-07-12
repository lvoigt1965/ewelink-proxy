#!/bin/bash
# eWeLink Webhook Proxy — Startup Script
# Run this on the host Ubuntu server

PORT=8182
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJ_DIR/ewelink-venv"

cd "$PROJ_DIR"

# Kill any existing process on the port
sudo lsof -ti:$PORT | xargs sudo kill -9 2>/dev/null; true

# Start the server
nohup "$VENV/bin/uvicorn" app:app --host 0.0.0.0 --port $PORT > server.log 2>&1 &

echo "eWeLink Webhook Proxy started on port $PORT"
echo "Web UI: http://localhost:$PORT/"
echo "Log: $PROJ_DIR/server.log"
