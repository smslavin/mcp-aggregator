#!/usr/bin/env bash
# Start all demo processes: mock backend + aggregator.
# Run opcua-mcp separately if you want OPC-UA tools too.

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$DIR/.venv/bin/python"

echo "Starting mock plant backend on port 8003..."
"$PYTHON" "$DIR/mock_backend.py" &
MOCK_PID=$!

sleep 2

echo "Starting MCP aggregator on port 8100..."
"$PYTHON" "$DIR/server.py" &
AGG_PID=$!

echo ""
echo "Aggregator: http://localhost:8100/sse"
echo 'Add to Claude Desktop config:'
echo '  "scada": { "url": "http://localhost:8100/sse" }'
echo ""
echo "PIDs: mock=$MOCK_PID aggregator=$AGG_PID"
echo "Press Ctrl+C to stop both."

wait
