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

# ── Configuration ──────────────────────────────────────────────────────────────

$Root          = $PSScriptRoot
$Port          = "8100"
$BackendsFile  = "backends.production.json"  # relative to $Root; use backends.json for demo

# ── End Configuration ──────────────────────────────────────────────────────────

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
nssm set $svcName Description "MCP Aggregator — unified SSE endpoint for all backend MCP servers (port $Port)"

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

nssm start $svcName
Start-Sleep -Milliseconds 500
$status = (Get-Service -Name $svcName).Status
Write-Host "$svcName — $status" -ForegroundColor Green
Write-Host "Logs: $LogDir"
