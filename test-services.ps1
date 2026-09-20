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
    else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($null -ne $command -and $command.Source -notlike '*\WindowsApps\python.exe') {
            $python = $command.Source
        }
    }
}

if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path -LiteralPath $python)) {
    Write-Error 'TEST-SERVICES REFUSED: Python was not found. Set ERP_TEST_PYTHON or create the project .venv.'
    exit 2
}

$arguments = @($runner)
if ($SelfTestFailure) {
    $arguments += '--self-test-failure'
}

& $python @arguments
exit $LASTEXITCODE
