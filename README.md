# mcp-aggregator

[![Tests](https://github.com/smslavin/mcp-aggregator/actions/workflows/test.yml/badge.svg)](https://github.com/smslavin/mcp-aggregator/actions/workflows/test.yml)

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
opcua__browse_nodes
opcua__read_nodes
opcua__search_nodes
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

**Startup:** connects to every backend in `backends.json` at once and calls
`tools/list`, giving each 15 s to answer. Every discovered tool is registered with a
`{backend}__{tool}` prefix so the routing is explicit and collision-free. Streamable
HTTP and stdio backends also start a persistent pooled session once they answer.

A backend that can't be reached at startup, such as a Windows service still
starting, is retried in the background every 2 s, backing off to 60 s, until it
answers; its tools then appear without a restart. `POST /backends/reload` hands
backends it can't reach to the same retry. `GET /backends` shows each one's status
and last error while it waits.

Whenever the tool list changes (a backend answers, is added, reloaded or removed),
the aggregator tells connected clients: `notifications/tools/list_changed` for
clients on the initialize handshake (it advertises `tools.listChanged`), and a
`subscriptions/listen` event for clients on the 2026-07-28 protocol.

**Runtime:** each tool call is routed to the originating backend. Streamable HTTP
and stdio backends use the persistent pooled session; a call made while it is
reconnecting waits up to 5 s for it, then reports the backend as reconnecting. SSE
backends open a fresh connection per call. Calls give up after 60 s. The aggregator
adds no parsing or transformation — it is a transparent proxy. A backend error
reaches the client with its real message, not anyio's "unhandled errors in a
TaskGroup".

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

## Running tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest tests/ -v
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

`backends.json` is the demo's list: the mock backend `start_demo` runs on 8003, under
the name `influxdb`. Set the `BACKENDS_FILE` environment variable to point at a
different file (relative to the script directory, or absolute path).

The Windows service uses `backends.production.json`. It is gitignored so each site
can edit it freely. `install_service.ps1` (and graccess-mcp's and mes-mcp's
installers) create it from `backends.production.example.json` if it doesn't exist,
and never overwrite an existing file. The example matches the demo VM: mes on 8007,
graccess on 8000, MQTT on 8003 (8001 is IIS there) and OPC-UA on 8002.

The aggregator checks the file at startup and refuses to start if an entry has no
name, a name containing `__` or ending in `_`, an unknown `transport`, no `url` (or
`command` for stdio), or a name listed twice. `POST /backends/reload` with such a file
returns 400 and changes nothing.

`start_demo.ps1` frees ports 8003 and 8100 by killing what listens there, so it
refuses to run where the AVEVA Demo services are installed; pass `-Force` to override.

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
    "include_tools": ["read_topics", "scan_topics"],
    "default_args": { "topic_filter": "Plant/WTP/Pump/RawWater_*" }
  },
  {
    "name": "mqtt_treated",
    "url": "http://localhost:8001/sse",
    "include_tools": ["read_topics", "scan_topics"],
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

### Stdio backends

Backends that are local subprocesses speaking MCP over stdio (e.g. the Rust
`fieldworks-adapters` binaries — `mqtt-mcp`, `opcua-mcp`) are declared with
`"transport": "stdio"` and `command`/`args` (and optionally `env`) instead of a `url`:

```json
[
  { "name": "mqtt", "transport": "stdio", "command": "mqtt-mcp", "args": ["--broker", "localhost:1883"] },
  { "name": "opcua", "transport": "stdio", "command": "/opt/fieldworks/opcua-mcp", "args": [] }
]
```

The aggregator spawns the subprocess and keeps it running for the aggregator's
lifetime — like Streamable HTTP, stdio backends use the persistent connection pool
rather than spawning a fresh process per call, since many adapters expose an explicit
`connect` tool that establishes state later calls depend on. If the pool session isn't
ready yet (or drops), a call waits up to 5 s for it and otherwise fails with "backend
is reconnecting"; it never runs in a one-off process, which would lack that state. The
pool reconnects on its own.

## Network access

The aggregator exposes every backend tool, including galaxy write tools, with no
confirmation step. It therefore listens on `127.0.0.1` by default. To reach it
from another machine, set both variables (in `.env` or the service environment):

```
AGGREGATOR_BIND_HOST=0.0.0.0
AGGREGATOR_AUTH_TOKEN=<long random string>
```

The aggregator refuses to start on a non-loopback address without a token. When a
token is set, every request (MCP transports and the management API) must send
`Authorization: Bearer <token>`, including requests from localhost. Clients on the
same machine, such as the chat UI, need the token too (`AGGREGATOR_AUTH_TOKEN` in
graccess-mcp's `chat_ui\.env`).

Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Open port 8100 in the firewall only when remote clients need it.

## Tool safety annotations and read-only mode

The aggregator forwards each backend tool's MCP annotations (`readOnlyHint`,
`destructiveHint`, ...) unchanged, so clients such as the graccess-mcp chat UI or
Claude Desktop can decide which calls need a human to confirm them.

Set `AGGREGATOR_READ_ONLY=1` to expose only tools that declare
`readOnlyHint=true`. Every other tool is hidden from `tools/list` and refused if
called, including tools with no annotations, because MCP treats a missing
annotation as "may write". Use it on shared or public machines where nobody
should be able to change the galaxy through the aggregator. Clients that go
through the aggregator (the chat UI included) become read-only as well.

## Management API

The aggregator exposes a runtime management API for adding and removing backends
without restarting. It follows the same token rule as the MCP endpoints.
`POST /backends` lets a caller register an arbitrary backend, so keep the token
secret and the port closed where it isn't needed. It only takes `sse` and
`streamable_http` backends, with `Content-Type: application/json`. A `stdio` backend
is a command the aggregator runs under its own account, so it can only come from the
backends file.

### Requests from web pages

With no token on a loopback bind, any program on the machine can call the
aggregator, including a web page open in a local browser. So the aggregator refuses
every request a browser makes on a page's behalf, on the MCP endpoints and the
management API alike:

- a request with an `Origin` header, unless that origin is listed in
  `AGGREGATOR_ALLOWED_ORIGINS`;
- a request marked `Sec-Fetch-Site: cross-site` or `same-site`;
- on a loopback bind, a `Host` other than `127.0.0.1`, `localhost` or `[::1]`, which
  stops DNS rebinding.

MCP clients, the site app and curl send none of these headers and are unaffected.
Opening `http://127.0.0.1:8100/backends` in the address bar still works. To use a
browser-based client such as the MCP Inspector, list its origin:

```
AGGREGATOR_ALLOWED_ORIGINS=http://localhost:6274
```

| Method | Path | Description |
|---|---|---|
| `GET` | `/backends` | List backends: `source` (file or api), `status` (pending or ready), tool count, pool status, `last_error`, and when the last discovery ran and how long it took |
| `POST` | `/backends` | Add a backend (same JSON shape as a backends.json entry) |
| `DELETE` | `/backends/{name}` | Remove a backend and deregister its tools |
| `POST` | `/backends/reload` | Re-read backends.json: add new entries, replace changed ones, remove gone ones. Backends added through `POST /backends` stay, unless the file names one |

Connected clients are told when the tool list changes (see "How it works"). A client
that ignores the notification sees the change the next time it lists tools.

### Example: add a backend at runtime

```bash
curl -X POST http://localhost:8100/backends \
  -H "Authorization: Bearer $AGGREGATOR_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "historian", "url": "http://192.168.1.50:8005/sse"}'
```

### Example: reload from backends.json

```bash
curl -X POST http://localhost:8100/backends/reload \
  -H "Authorization: Bearer $AGGREGATOR_AUTH_TOKEN"
```

Omit the `Authorization` header if no token is set.

## Running as a Windows service

The aggregator can run as an auto-start Windows service via [NSSM](https://nssm.cc/download).
The service depends on no backend service: a backend that isn't up yet is retried until it
answers, and its tools then appear.

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
      "args": [
        "-y", "mcp-remote", "http://<aggregator-host>:8100/mcp", "--allow-http",
        "--header", "Authorization:${AUTH_HEADER}"
      ],
      "env": { "AUTH_HEADER": "Bearer <token>" }
    }
  }
}
```

The header value goes through `env` because Claude Desktop on Windows mangles
spaces inside `args`. Drop the `--header` pair and `env` when the aggregator has
no token (localhost only).

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