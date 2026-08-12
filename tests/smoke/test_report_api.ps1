param(
    [string]$ProjectRoot = "D:\n8n_AI",
    [string]$ApiBaseUrl = "http://127.0.0.1:8090",
    [string]$TaskId = "vehicle-demo-001",
    [int]$TimeoutSec = 10
)

$ErrorActionPreference = "Stop"
$testData = Join-Path $ProjectRoot "test-data"

try {
    $healthJson = & curl.exe `
        --silent `
        --show-error `
        --fail `
        --noproxy "*" `
        --max-time $TimeoutSec `
        "$ApiBaseUrl/health"
    if ($LASTEXITCODE -ne 0) {
        throw "curl health check failed with exit code $LASTEXITCODE"
    }
    $health = $healthJson | ConvertFrom-Json
} catch {
    throw (
        "Report API is unavailable at $ApiBaseUrl. " +
        "Start uvicorn on port 8090 before running this script. " +
        "Original error: $($_.Exception.Message)"
    )
}

if ($health.status -ne "ok") {
    throw "Report API health check returned an unexpected status: $($health.status)"
}

function Send-VersionFile {
    param(
        [string]$FileName,
        [string]$MessageId
    )

    $path = Join-Path $testData $FileName
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Test file was not found: $path"
    }
    $body = @{
        status = "success"
        event_id = "evt-$MessageId"
        message_id = $MessageId
        file_name = $FileName
        extracted_text = Get-Content -LiteralPath $path -Raw -Encoding UTF8
        error = $null
        task_id = $TaskId
        version_role = "auto"
    } | ConvertTo-Json -Depth 8

    $tempBodyPath = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("report-api-body-" + [guid]::NewGuid().ToString("N") + ".json")
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempBodyPath, $body, $utf8NoBom)

    try {
        $responseJson = & curl.exe `
            --silent `
            --show-error `
            --fail `
            --noproxy "*" `
            --max-time $TimeoutSec `
            --request POST `
            --header "Content-Type: application/json; charset=utf-8" `
            --data-binary "@$tempBodyPath" `
            "$ApiBaseUrl/n8n/version-file"
        if ($LASTEXITCODE -ne 0) {
            throw "curl POST failed with exit code $LASTEXITCODE"
        }
        return ($responseJson | ConvertFrom-Json)
    } finally {
        Remove-Item -LiteralPath $tempBodyPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Sending the old version..."
$oldResult = Send-VersionFile -FileName "vehicle_spec_v1.txt" -MessageId "om_mock_old"
$oldResult | ConvertTo-Json -Depth 10

Write-Host "Sending the new version..."
$newResult = Send-VersionFile -FileName "vehicle_spec_v2.txt" -MessageId "om_mock_new"
$newResult | ConvertTo-Json -Depth 10

if ($oldResult.status -ne "waiting_for_pair") {
    throw "Unexpected old-version response: $($oldResult.status)"
}
if ($newResult.status -ne "completed") {
    throw "New-version analysis did not complete: $($newResult.status)"
}

Write-Host "report_pipeline_api pairing and analysis smoke test passed."
