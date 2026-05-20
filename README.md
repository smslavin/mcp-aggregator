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

Tools are prefixed `{backend_name}__{tool_name}`. With backends named `graccess`,
`mqtt`, and `opcua`, the aggregated namespace looks like:

```
graccess__connect_galaxy
graccess__list_galaxies
graccess__set_attribute
...
mqtt__list_topics
mqtt__read_topic_value
...
opcua__browse_nodes
opcua__read_node
...
```

The prefix is stripped before forwarding to the backend, so backend servers receive
the original tool name unchanged.

In environments with multiple data sources — OPC-UA, MQTT, SCADA configuration — namespacing 
prevents tool name collisions and makes the audit log immediately readable. 
Every tool call identifies both the domain and the operation.

## How it works

**Startup:** for each backend in the configured backends file (see below), opens an SSE session and calls
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
# macOS / Linux (make executable once, then run)
chmod +x start_demo.sh
./start_demo.sh
```

The aggregator runs on port 8100 by default. Set `AGGREGATOR_PORT` in `.env` to change.

## backends.json / backends.production.json

The aggregator reads a JSON file listing backend servers at startup. By default it reads
`backends.json`. Set the `BACKENDS_FILE` environment variable to use a different file
(relative to the script directory, or absolute path).

**`backends.json`** — demo config (mock plant backend, no AVEVA required):
```json
[
  { "name": "opcua", "url": "http://localhost:8002/sse" },
  { "name": "plant", "url": "http://localhost:8003/sse" }
]
```

**`backends.production.json`** — production config (full graccess-mcp stack):
```json
[
  { "name": "graccess", "url": "http://127.0.0.1:8000/sse" },
  { "name": "mqtt",     "url": "http://127.0.0.1:8001/sse" },
  { "name": "opcua",    "url": "http://127.0.0.1:8002/sse" }
]
```

If a backend is unreachable at startup, its tools are silently skipped and a warning
is logged. The aggregator still starts with whatever tools it could discover.

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

`install_service.ps1` defaults to `backends.production.json`. Edit the `$BackendsFile`
variable at the top of the script to use a different config. The service is installed as
`AVEVA Demo McpAggregator` on port 8100.

When using the full graccess-mcp stack, the coordinated `install_services.ps1` in the
graccess-mcp repo installs all seven services (backends, aggregator, chat UI) in the
correct order with service dependencies wired automatically.

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