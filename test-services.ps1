[CmdletBinding()]
param(
    [switch]$SelfTestFailure
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$runner = Join-Path $projectRoot 'scripts\test_services.py'

$python = $env:ERP_TEST_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) {
    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $projectPython) {
        $python = $projectPython
    }
}

if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path -LiteralPath $python)) {
    Write-Error 'TEST-SERVICES REFUSED: Project .venv is not configured. Run scripts/setup_dev.ps1 or set ERP_TEST_PYTHON to an explicit test Python.' -ErrorAction Continue
    exit 2
}

$arguments = @($runner)
if ($SelfTestFailure) {
    $arguments += '--self-test-failure'
}

Write-Output "TEST-SERVICES Python: $python"
& $python @arguments
exit $LASTEXITCODE
