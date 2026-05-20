# Start all demo processes: mock backend + aggregator.
# Run opcua-mcp separately if you want OPC-UA tools too.

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = "$dir\.venv\Scripts\python.exe"

Write-Host "Starting mock plant backend on port 8003..."
Start-Process -FilePath $venv -ArgumentList "mock_backend.py" -WorkingDirectory $dir -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "Starting MCP aggregator on port 8100..."
Start-Process -FilePath $venv -ArgumentList "server.py" -WorkingDirectory $dir -WindowStyle Normal

Write-Host ""
Write-Host "Aggregator: http://localhost:8100/sse"
Write-Host "Add to Claude Desktop config:"
Write-Host '  "scada": { "url": "http://localhost:8100/sse" }'
