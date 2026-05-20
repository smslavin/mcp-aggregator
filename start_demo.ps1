# Start all demo processes: mock backend + aggregator.
# Each runs in its own PowerShell window that stays open so errors are visible.
# Run opcua-mcp separately if you want OPC-UA tools too.

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = "$dir\.venv\Scripts\python.exe"

Write-Host "Starting mock plant backend on port 8003..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv' '$dir\mock_backend.py'" -WorkingDirectory $dir

Start-Sleep -Seconds 2

Write-Host "Starting MCP aggregator on port 8100..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv' '$dir\server.py'" -WorkingDirectory $dir

Write-Host ""
Write-Host "Aggregator: http://localhost:8100/mcp"
Write-Host "Add to Claude Desktop config:"
Write-Host '  "scada": { "url": "http://localhost:8100/mcp" }'
