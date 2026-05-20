# Start all demo processes: mock backend + aggregator.
# Each runs in its own PowerShell window that stays open so errors are visible.
# Run opcua-mcp separately if you want OPC-UA tools too.

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = "$dir\.venv\Scripts\python.exe"

function Stop-PortProcess($port) {
    $hits = netstat -ano | Select-String "[:.]$port\s+\S+\s+LISTENING"
    foreach ($line in $hits) {
        $portPid = ($line.ToString().Trim() -split '\s+')[-1]
        if ($portPid -match '^\d+$' -and $portPid -ne '0') {
            Write-Host "  Stopping PID $portPid on port $port"
            Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "Clearing ports 8003 and 8100..."
Stop-PortProcess 8003
Stop-PortProcess 8100
Start-Sleep -Seconds 1

Write-Host "Starting mock plant backend on port 8003..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv' '$dir\mock_backend.py'" -WorkingDirectory $dir

Start-Sleep -Seconds 2

Write-Host "Starting MCP aggregator on port 8100..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv' '$dir\server.py'" -WorkingDirectory $dir

Write-Host ""
Write-Host "Aggregator: http://localhost:8100/mcp"
Write-Host "Add to Claude Desktop config:"
Write-Host '  "scada": { "url": "http://localhost:8100/mcp" }'
