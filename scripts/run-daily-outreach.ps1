param(
    [string]$Python = (Join-Path $PSScriptRoot "..\.venv312\Scripts\python.exe")
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    & $Python -m app.daily_outreach
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
