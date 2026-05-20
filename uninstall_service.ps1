#Requires -RunAsAdministrator
$svc = Get-Service -Name "AVEVA Demo McpAggregator" -ErrorAction SilentlyContinue
if (-not $svc) { Write-Host "AVEVA Demo McpAggregator not installed."; exit 0 }
if ($svc.Status -eq "Running") { nssm stop "AVEVA Demo McpAggregator" confirm }
nssm remove "AVEVA Demo McpAggregator" confirm
Write-Host "AVEVA Demo McpAggregator removed."
