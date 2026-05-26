# mcp-aggregator

Single endpoint that aggregates multiple backend MCP servers into one unified tool namespace.

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

In environments with multiple data sources — OPC-UA, MQTT, SCADA configuration — namespacing 
prevents tool name collisions and makes the audit log immediately readable. 
Every tool call identifies both the domain and the operation.

## How it works

**Startup:** for each backend in `backends.json`, connects to the backend and calls
`tools/list`. Every discovered tool is registered with a `{backend}__{tool}` prefix so
the routing is explicit and collision-free. Streamable HTTP backends also start a
persistent pooled session at this point.

**Runtime:** each tool call is routed to the originating backend. Streamable HTTP
backends use the persistent pooled session; SSE backends open a fresh connection per
call. The aggregator adds no parsing or transformation — it is a transparent proxy.

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
# macOS / Linux (make executable once, then run)
chmod +x start_demo.sh
./start_demo.sh
```

The aggregator runs on port 8100 by default. Set `AGGREGATOR_PORT` in `.env` to change.

## backends.json

Edit to add or remove backends. The aggregator reads this at startup. Set the
`BACKENDS_FILE` environment variable to point at a different file (relative to the
script directory, or absolute path) — useful for keeping separate demo and production
configs without modifying `backends.json`.

```json
[
  { "name": "opcua", "url": "http://localhost:8002/sse" },
  { "name": "plant", "url": "http://localhost:8003/sse" }
]
```

If a backend is unreachable at startup, its tools are silently skipped and a warning
is logged. The aggregator still starts with whatever tools it could discover.

### Tool filtering

Use `include_tools` or `exclude_tools` to control which tools are exposed from a
backend. Filtering happens at discovery time — filtered tools never appear in the
aggregated namespace. The two fields are mutually exclusive; using both on the same
backend is an error.

```json
[
  {
    "name": "influxdb",
    "url": "http://localhost:8003/sse",
    "include_tools": ["query", "list_measurements"]
  },
  {
    "name": "opcua",
    "url": "http://localhost:8002/sse",
    "exclude_tools": ["dangerous_write_tool"]
  }
]
```

### Default arguments

Use `default_args` to inject argument defaults into every tool call forwarded to a
backend. Caller-supplied arguments always take precedence over defaults. This is useful
for scoping a backend to a specific context — for example, pointing two MQTT backends
at the same server but restricting each to a different topic namespace:

```json
[
  {
    "name": "mqtt_rawwater",
    "url": "http://localhost:8001/sse",
    "include_tools": ["read_topic_value", "scan_topics"],
    "default_args": { "topic_filter": "Plant/WTP/Pump/RawWater_*" }
  },
  {
    "name": "mqtt_treated",
    "url": "http://localhost:8001/sse",
    "include_tools": ["read_topic_value", "scan_topics"],
    "default_args": { "topic_filter": "Plant/WTP/Pump/Treated_*" }
  }
]
```

### Streamable HTTP backends

Backends that serve the newer Streamable HTTP transport can be declared with
`"transport": "streamable_http"`. The default is `"sse"` if the field is absent.

```json
[
  { "name": "opcua", "url": "http://localhost:8002/sse" },
  { "name": "analytics", "url": "http://localhost:8004/mcp", "transport": "streamable_http" }
]
```

Streamable HTTP backends benefit from connection pooling — the aggregator keeps one
persistent `ClientSession` open per backend and routes all calls through it. SSE
backends still open a fresh connection per call.

## Management API

The aggregator exposes a runtime management API for adding and removing backends
without restarting. All endpoints are unauthenticated.

> **OT environment note:** In a live operational technology environment, expose the
> management API only on a trusted network interface or behind a reverse proxy with
> access controls. Unauthenticated `POST /backends` allows any caller to register
> an arbitrary backend server.

| Method | Path | Description |
|---|---|---|
| `GET` | `/backends` | List active backends, tool counts, pool status |
| `POST` | `/backends` | Add a backend (same JSON shape as a backends.json entry) |
| `DELETE` | `/backends/{name}` | Remove a backend and deregister its tools |
| `POST` | `/backends/reload` | Re-read backends.json, add new entries, remove gone ones |

**Note:** MCP clients (including Claude Desktop) call `tools/list` once at connect
time and cache the result. Adding or removing a backend takes effect for new client
connections but won't be visible to already-connected clients until they reconnect.

### Example: add a backend at runtime

```bash
curl -X POST http://localhost:8100/backends \
  -H "Content-Type: application/json" \
  -d '{"name": "historian", "url": "http://192.168.1.50:8005/sse"}'
```

### Example: reload from backends.json

```bash
curl -X POST http://localhost:8100/backends/reload
```

## Running as a Windows service

The aggregator can run as an auto-start Windows service via [NSSM](https://nssm.cc/download).
Start the three backend MCP servers before starting the aggregator — tool discovery runs at
startup and backends that are unreachable at that point will not have their tools registered.

```powershell
# Install (run as Administrator)
.\install_service.ps1

# Remove
.\uninstall_service.ps1
```

Edit the `$BackendsFile` variable at the top of `install_service.ps1` to point at your
backends config. The service is installed as `AVEVA Demo McpAggregator` on port 8100.

## Claude Desktop config

Claude Desktop requires HTTPS for URL-based remote connectors and rejects plain HTTP
entries at config validation. The workaround is [`mcp-remote`](https://www.npmjs.com/package/mcp-remote),
a lightweight npm bridge that runs as a local stdio subprocess and connects to the
aggregator over HTTP internally.

**Prerequisite:** Node.js on the client machine (`winget install OpenJS.NodeJS` or
[nodejs.org](https://nodejs.org)).

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "scada": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://<aggregator-host>:8100/mcp", "--allow-http"]
    }
  }
}
```

Replace `<aggregator-host>` with the IP or hostname of the machine running the aggregator
(e.g. `192.168.80.134`). Use `localhost` if Claude Desktop and the aggregator are on the
same machine.

The `--allow-http` flag is required because `mcp-remote` also enforces HTTPS by default
for non-localhost URLs.

The aggregator serves two transports on the same port:

| Path | Transport | Use for |
|---|---|---|
| `/mcp` | Streamable HTTP | Claude Desktop (via mcp-remote), modern MCP clients |
| `/sse` | Legacy SSE | Older clients, custom chat UIs |