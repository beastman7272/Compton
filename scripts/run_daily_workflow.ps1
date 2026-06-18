param(
    [ValidateSet("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
    [string]$RunDay = "",

    [switch]$ImportDryRun,
    [switch]$NoSheetUpdate
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$DefaultPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$Python = if (Test-Path $DefaultPython) { $DefaultPython } else { "python" }

$LogDir = Join-Path $ProjectRoot "logs\daily-workflow"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "$Timestamp.log"

function Write-Log {
    param([string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

function Invoke-PythonStep {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    Write-Log ""
    Write-Log "START: $Name"
    Write-Log "COMMAND: $Python $($Arguments -join ' ')"

    Push-Location $ProjectRoot
    try {
        & $Python @Arguments *>&1 | Tee-Object -FilePath $LogPath -Append
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        Write-Log "FAILED: $Name exited with code $exitCode"
        exit $exitCode
    }

    Write-Log "DONE: $Name"
}

function Add-RunDayArgs {
    param([string[]]$Arguments)

    if ($RunDay) {
        return $Arguments + @("--run-day", $RunDay)
    }

    return $Arguments
}

Write-Log "Daily workflow started"
Write-Log "Project root: $ProjectRoot"
Write-Log "Python: $Python"
if ($RunDay) {
    Write-Log "Run day override: $RunDay"
}

Invoke-PythonStep "BuildingConnected login check" @("bid_board_orchestrator.py", "--check-buildingconnected-login")
Invoke-PythonStep "ConstructConnect email processor" (Add-RunDayArgs @("construct_connect_processor.py"))
Invoke-PythonStep "ConstructConnect Playwright workflow" (Add-RunDayArgs @("construct_connect_playwright.py", "--non-interactive"))
Invoke-PythonStep "Stage 1 email processor" @("stage1_email_processor.py")
Invoke-PythonStep "BuildingConnected workflow" @("bid_board_orchestrator.py", "--run-playwright-workflow")

$ImportArgs = Add-RunDayArgs @("scripts\run_import.py")
if ($ImportDryRun) {
    $ImportArgs += "--dry-run"
}
if ($NoSheetUpdate) {
    $ImportArgs += "--no-sheet-update"
}
Invoke-PythonStep "CQE import workflow" $ImportArgs

Write-Log ""
Write-Log "Daily workflow completed successfully"
Write-Log "Log saved to: $LogPath"
