#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the MCP Aggregator as a Windows service via NSSM.

.DESCRIPTION
    Installs one service: AVEVA Demo McpAggregator (port 8100).
    NSSM must be on PATH (https://nssm.cc/download). Run as Administrator.

    The aggregator discovers tools from its backend MCP servers at startup.
    Start graccess-mcp (8000), mqtt-mcp (8001), and opcua-mcp (8002) before
    starting this service, or tool discovery for unavailable backends will be
    skipped (logged as errors, not fatal).

.EXAMPLE
    # Edit the Configuration block below, then:
    .\install_service.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- Configuration --------------------------------------------------------------

$Root          = $PSScriptRoot
$Port          = "8100"
$BackendsFile  = "backends.production.json"  # relative to $Root; use backends.json for demo
# Backend services to start before the aggregator. Ones not installed are skipped.
$BackendServices = @("AVEVA Demo GRAccessMCP", "AVEVA Demo MqttMCP", "AVEVA Demo OpcuaMCP")

# -- End Configuration ----------------------------------------------------------

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "nssm not found on PATH. Download from https://nssm.cc/download and add to PATH."
}

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$svcName = "AVEVA Demo McpAggregator"
$exe     = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "Installing $svcName ..." -ForegroundColor Cyan

$existing = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -eq "Running") { nssm stop $svcName confirm }
    nssm remove $svcName confirm
}

nssm install $svcName $exe "server.py"
nssm set $svcName AppDirectory $Root
nssm set $svcName Description "MCP Aggregator - unified SSE endpoint for all backend MCP servers (port $Port)"

$envBlock = "AGGREGATOR_PORT=$Port`nBACKENDS_FILE=$BackendsFile"
nssm set $svcName AppEnvironmentExtra $envBlock

nssm set $svcName AppStdout (Join-Path $LogDir "mcp-aggregator-stdout.log")
nssm set $svcName AppStderr (Join-Path $LogDir "mcp-aggregator-stderr.log")
nssm set $svcName AppStdoutCreationDisposition 4
nssm set $svcName AppStderrCreationDisposition 4
nssm set $svcName AppRotateFiles 1
nssm set $svcName AppRotateBytes 10485760

nssm set $svcName AppExit Default Restart
nssm set $svcName AppRestartDelay 60000
nssm set $svcName Start SERVICE_AUTO_START

# Tool discovery runs once at startup, so backends should be started first.
# Written to the registry because nssm set can't take multiple service names
# containing spaces. Re-run this script after installing a missing backend.
$deps    = @($BackendServices | Where-Object { Get-Service -Name $_ -ErrorAction SilentlyContinue })
$missing = @($BackendServices | Where-Object { $deps -notcontains $_ })
if ($deps.Count -gt 0) {
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$svcName" -Name DependOnService -Type MultiString -Value $deps
    Write-Host "Dependencies set: $($deps -join ', ')"
}
if ($missing.Count -gt 0) {
    Write-Warning "Not installed, no dependency set: $($missing -join ', '). Re-run after installing them."
}

nssm start $svcName
Start-Sleep -Milliseconds 500
$status = (Get-Service -Name $svcName).Status
Write-Host "$svcName - $status" -ForegroundColor Green
Write-Host "Logs: $LogDir"
