param(
    [string]$ProjectRoot = "D:\n8n_AI",
    [string]$ApiBaseUrl = "http://127.0.0.1:8090",
    [string]$TaskId = "",
    [int]$TimeoutSec = 10,
    [string]$PythonExecutable = "E:\conda_envs\n8n-ai\python.exe"
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "report_api_smoke.py"

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $PythonExecutable"
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Smoke helper was not found: $scriptPath"
}

$arguments = @(
    $scriptPath,
    "--project-root", $ProjectRoot,
    "--api-base-url", $ApiBaseUrl,
    "--timeout-sec", $TimeoutSec
)
if ($TaskId) {
    $arguments += @("--task-id", $TaskId)
}

& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) {
    throw "report API smoke test failed with exit code $LASTEXITCODE"
}
