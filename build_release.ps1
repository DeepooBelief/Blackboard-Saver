[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$BuildVenv = Join-Path $RepoRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$Requirements = Join-Path $RepoRoot "requirements.txt"
$SpecFile = Join-Path $RepoRoot "BlackboardSaver.spec"
$BuildDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"
$ExePath = Join-Path $DistDir "BlackboardSaver.exe"
$SmokeLog = Join-Path $DistDir "BlackboardSaver.smoke.log"
$HashPath = Join-Path $DistDir "BlackboardSaver.exe.sha256"

function Remove-PathWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return
    }

    for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force
            return
        }
        catch {
            if ($Attempt -eq 5) {
                throw "Could not remove '$Path'. Close any running BlackboardSaver.exe window and try again. $($_.Exception.Message)"
            }
            Start-Sleep -Seconds 1
        }
    }
}

if (-not (Test-Path $BuildPython)) {
    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonLauncher) {
        & $PythonLauncher.Source -3.10 -m venv $BuildVenv
    }
    else {
        & python -m venv $BuildVenv
    }
}

& $BuildPython -m pip install --upgrade pip
& $BuildPython -m pip install -r $Requirements "pyinstaller>=6.0"

Remove-PathWithRetry $BuildDir
Remove-PathWithRetry $DistDir

& $BuildPython -m PyInstaller --clean --noconfirm $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

if (-not (Test-Path $ExePath)) {
    throw "Expected executable was not created: $ExePath"
}

& $ExePath --smoke-test *> $SmokeLog
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $SmokeLog) {
        Get-Content $SmokeLog
    }
    throw "Built executable failed its smoke check."
}

$Hash = Get-FileHash -LiteralPath $ExePath -Algorithm SHA256
"$($Hash.Hash)  BlackboardSaver.exe" | Set-Content -LiteralPath $HashPath -Encoding ASCII

Write-Host "Built $ExePath"
Write-Host "Wrote $HashPath"
