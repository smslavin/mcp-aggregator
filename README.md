# mcp-aggregator

Single SSE endpoint that aggregates multiple backend MCP servers into one unified tool namespace.

```
AI Client (Claude Desktop / Chat UI)
         │ SSE :8100
         ▼
    mcp-aggregator          ← this server
    ├── startup: discover tools from all backends
    ├── runtime: route + proxy tool calls
    └── cross-cutting: logging, error isolation
         │                    │
    SSE :8002            SSE :8003
    opcua-mcp            mock-backend (or any FastMCP server)
```

## How it works

**Startup:** for each backend in `backends.json`, opens an SSE session and calls
`tools/list`. Every discovered tool is registered with a `{backend}__{tool}` prefix so
the routing is explicit and collision-free.

**Runtime:** each tool call opens a fresh SSE session to the originating backend,
calls the tool, and returns the result. The aggregator adds no parsing or transformation
— it is a transparent proxy.

## Setup

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

Start all backends first, then the aggregator.

**Windows (PowerShell)**
```powershell
# Terminal 1 — mock plant backend (demo, no AVEVA required)
.venv\Scripts\python mock_backend.py

# Terminal 2 — opcua-mcp (optional, needs OPC-UA simulator or server)
cd ..\opcua-mcp && .venv\Scripts\python server.py

# Terminal 3 — aggregator
.venv\Scripts\python server.py
```

**macOS / Linux**
```bash
# Terminal 1 — mock plant backend (demo, no AVEVA required)
.venv/bin/python mock_backend.py

# Terminal 2 — opcua-mcp (optional, needs OPC-UA simulator or server)
cd ../opcua-mcp && .venv/bin/python server.py

# Terminal 3 — aggregator
.venv/bin/python server.py
```

Or use the launcher scripts:

```powershell
# Windows
.\start_demo.ps1
```
```bash
# macOS / Linux
./start_demo.sh
```

The aggregator runs on port 8100 by default. Set `AGGREGATOR_PORT` in `.env` to change.

## backends.json

Edit to add or remove backends. The aggregator reads this at startup.

```json
[
  { "name": "opcua", "url": "http://localhost:8002/sse" },
  { "name": "plant", "url": "http://localhost:8003/sse" }
]
```

If a backend is unreachable at startup, its tools are silently skipped and a warning
is logged. The aggregator still starts with whatever tools it could discover.

## Claude Desktop config

```json
{
  "mcpServers": {
    "scada": { "url": "http://localhost:8100/sse" }
  }
}
```

One entry. All tools from all backends.

## Tool namespacing

Tools are prefixed `{backend_name}__{tool_name}`. With backends named `opcua` and
`plant`, the aggregated namespace looks like:

```
opcua__connect_server
opcua__browse_nodes
opcua__read_node
...
plant__get_plant_status
plant__list_sensors
plant__acknowledge_alarm
...
```

The prefix is stripped before forwarding to the backend, so backend servers receive
the original tool name unchanged.
