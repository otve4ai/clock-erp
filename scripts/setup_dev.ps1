[CmdletBinding()]
param(
    [string]$PythonPath = $env:ERP_DEV_PYTHON
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$requirements = Join-Path $projectRoot 'requirements-test-services.txt'

function Get-PythonVersion {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    $version = & $Executable @PrefixArguments -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))'
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        throw "Unable to determine the Python version for '$Executable'."
    }

    return [version]$version
}

function Assert-SupportedPython {
    param([version]$Version)

    if ($Version.Major -ne 3 -or $Version.Minor -ne 10) {
        throw "Python $Version is not supported for local development. Install Python 3.10 (the CI version)."
    }
}

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "Local test-services requirements were not found at '$requirements'."
}

$createdVenv = $false
if (Test-Path -LiteralPath $venvRoot) {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Existing .venv is incomplete. Move it aside manually, then run this script again."
    }
    $projectPythonVersion = Get-PythonVersion -Executable $venvPython
    Assert-SupportedPython -Version $projectPythonVersion
    Write-Output "Using existing project .venv (Python $projectPythonVersion)."
}
else {
    $basePython = $null
    $baseArguments = @()

    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        if (-not (Test-Path -LiteralPath $PythonPath)) {
            throw "ERP_DEV_PYTHON/PythonPath does not exist: '$PythonPath'."
        }
        $basePython = [System.IO.Path]::GetFullPath($PythonPath)
    }
    else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $launcher) {
            $basePython = $launcher.Source
            $baseArguments = @('-3.10')
        }
        else {
            $command = Get-Command python -ErrorAction SilentlyContinue
            if ($null -ne $command -and $command.Source -notlike '*\WindowsApps\python.exe') {
                $basePython = $command.Source
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($basePython)) {
        throw 'Compatible Python was not found. Install Python 3.10 or set ERP_DEV_PYTHON to its python.exe.'
    }

    $basePythonVersion = Get-PythonVersion -Executable $basePython -PrefixArguments $baseArguments
    Assert-SupportedPython -Version $basePythonVersion
    Write-Output "Creating project .venv with Python $basePythonVersion..."
    & $basePython @baseArguments -m venv $venvRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw 'Python failed to create the project .venv.'
    }
    $createdVenv = $true
}

try {
    Write-Output 'Installing the local test-services requirements...'
    & $venvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. The project .venv was not reported as ready.'
    }

    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw 'pip check found an inconsistent project .venv.'
    }
}
catch {
    if ($createdVenv -and (Test-Path -LiteralPath $venvRoot)) {
        Remove-Item -LiteralPath $venvRoot -Recurse -Force
    }
    throw
}

Write-Output ''
Write-Output 'Project .venv is ready.'
Write-Output "Python: $venvPython"
Write-Output "Activate: $venvRoot\Scripts\Activate.ps1"
