# HHOF Goalie Simulator - Goal Detector Windows Service Installer

$ErrorActionPreference = "Stop"

# -- Self-elevate if not running as admin ---------------------------------

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Relaunching as Administrator..."
    Start-Process powershell "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# -- Helpers --------------------------------------------------------------

function Write-Step { param($msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "   ERROR: $msg" -ForegroundColor Red; Read-Host "`nPress Enter to exit"; exit 1 }

# -- Verify we're in the repo root ----------------------------------------

$RepoRoot = $PSScriptRoot
if (-not (Test-Path "$RepoRoot\goal_detector.py")) {
    Write-Fail "Run this script from the goal-detection repo root"
}

# -- Prompts --------------------------------------------------------------

Write-Host "`nHHOF Goal Detector - Service Installer" -ForegroundColor Green
Write-Host "Press Enter to accept the default shown in brackets.`n"

$ServerUrl = Read-Host "WebSocket server URL  [ws://localhost:8765]"
if (-not $ServerUrl) { $ServerUrl = "ws://localhost:8765" }

$NetId = Read-Host "Net ID                [net_1]"
if (-not $NetId) { $NetId = "net_1" }

$ServiceName = Read-Host "Service name          [hhof-goal-detector]"
if (-not $ServiceName) { $ServiceName = "hhof-goal-detector" }

$UseGigE = Read-Host "Using Lucid GigE camera? (y/n)  [y]"
if (-not $UseGigE) { $UseGigE = "y" }
$UseGigE = $UseGigE.Trim().ToLower() -eq "y"

# -- Install uv -----------------------------------------------------------

Write-Step "Checking uv..."

$UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
if (-not (Test-Path $UvPath)) {
    $UvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
}

if (-not $UvPath -or -not (Test-Path $UvPath)) {
    Write-Host "   Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $UvPath = "$env:USERPROFILE\.local\bin\uv.exe"
}

if (-not (Test-Path $UvPath)) {
    Write-Fail "uv installation failed - install manually from https://docs.astral.sh/uv"
}
Write-Ok "uv at $UvPath"

# -- Install Python dependencies ------------------------------------------

Write-Step "Installing Python dependencies..."
& $UvPath sync --project $RepoRoot
if ($LASTEXITCODE -ne 0) { Write-Fail "uv sync failed" }
Write-Ok "Core dependencies ready (numpy, opencv-python, websockets)"

# -- Check Lucid Vision Arena SDK (native DLLs) ---------------------------
# arena_api (Python) is installed by uv sync above. It wraps the Arena SDK
# C++ DLLs via ctypes - those must be installed separately from Lucid's site.

if ($UseGigE) {
    # -- Arena SDK C++ DLLs -----------------------------------------------
    Write-Step "Checking Arena SDK native DLLs..."

    $ArenaDllDir = "C:\Program Files\Lucid Vision Labs\Arena SDK\x64Release"
    while (-not (Test-Path "$ArenaDllDir\ArenaC_v140.dll")) {
        Write-Warn "Arena SDK (C++ runtime) not found."
        Write-Host ""
        Write-Host "   You need to install TWO things from Lucid's downloads page:" -ForegroundColor Yellow
        Write-Host "     1. Arena SDK  (the main C++ installer, e.g. ArenaSDK_Windows.exe)" -ForegroundColor Yellow
        Write-Host "     2. arena_api  (the Python wheel, e.g. arena_api-X.Y.Z-py3-none-any.whl)" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "   Opening Lucid downloads page now..." -ForegroundColor Cyan
        Start-Process "cmd" "/c start https://thinklucid.com/downloads-hub/"
        Write-Host ""
        Write-Host "   Download and run the Arena SDK installer, then press Enter to continue." -ForegroundColor Yellow
        Write-Host "   (Keep the arena_api .whl file handy -- we will ask for it next.)" -ForegroundColor Yellow
        Write-Host "   Press S to skip GigE setup entirely." -ForegroundColor Yellow
        $key = Read-Host "   [Enter / S]"
        if ($key.Trim().ToLower() -eq "s") {
            Write-Warn "Skipping Arena SDK - GigE camera will not work until it is installed"
            $UseGigE = $false
            break
        }
    }
    if (Test-Path "$ArenaDllDir\ArenaC_v140.dll") {
        Write-Ok "Arena SDK DLLs found at $ArenaDllDir"
    }

    # -- arena_api Python package -----------------------------------------
    if ($UseGigE) {
        Write-Step "Installing arena_api Python package..."

        # Try pip install directly first - works if Lucid's SDK installer
        # registered it, or if it becomes available on PyPI in the future.
        $ErrorActionPreference = "Continue"
        & $UvPath pip install --python "$RepoRoot\.venv\Scripts\python.exe" arena_api 2>$null | Out-Null
        $arenaApiOk = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = "Stop"

        if ($arenaApiOk) {
            Write-Ok "arena_api installed"
        } else {
            # pip install failed - search for a wheel the user may have downloaded
            $SearchDirs = @(
                "C:\Program Files\Lucid Vision Labs\Arena SDK",
                "$env:USERPROFILE\Downloads",
                "$env:USERPROFILE\Desktop",
                $RepoRoot
            )
            $Wheel = $null
            foreach ($dir in $SearchDirs) {
                $Wheel = Get-ChildItem -Path $dir -Filter "arena_api*.whl" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($Wheel) { break }
            }

            if ($Wheel) {
                Write-Host "   Found wheel: $($Wheel.Name)"
                & $UvPath pip install --python "$RepoRoot\.venv\Scripts\python.exe" $Wheel.FullName
                if ($LASTEXITCODE -ne 0) { Write-Fail "arena_api install failed" }
                Write-Ok "arena_api installed from $($Wheel.Name)"
            } else {
                Write-Warn "Could not install arena_api automatically."
                Write-Host ""
                Write-Host "   On the Lucid downloads page, look for a Python SDK or ArenaPy download." -ForegroundColor Yellow
                Write-Host "   Download it, then re-run this installer." -ForegroundColor Yellow
                Write-Host "   Or press S to skip and set up the GigE camera manually later." -ForegroundColor Yellow
                Write-Host ""
                $key = Read-Host "   [Enter to retry / S to skip]"
                if ($key.Trim().ToLower() -ne "s") {
                    # Re-search after user has had time to download
                    foreach ($dir in $SearchDirs) {
                        $Wheel = Get-ChildItem -Path $dir -Filter "arena_api*.whl" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                        if ($Wheel) { break }
                    }
                    if ($Wheel) {
                        & $UvPath pip install --python "$RepoRoot\.venv\Scripts\python.exe" $Wheel.FullName
                        if ($LASTEXITCODE -ne 0) { Write-Fail "arena_api install failed" }
                        Write-Ok "arena_api installed from $($Wheel.Name)"
                    } else {
                        Write-Warn "Still not found - skipping. Re-run installer after downloading arena_api."
                    }
                }
            }
        }
    }
} else {
    Write-Step "Skipping Arena SDK check (USB camera mode)"
    Write-Ok "Not required"
}

# -- Install NSSM ---------------------------------------------------------

Write-Step "Checking NSSM..."

$NssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source

if (-not $NssmPath) {
    Write-Host "   Installing NSSM via winget..."
    winget install NSSM.NSSM --silent --accept-package-agreements --accept-source-agreements

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $NssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
}

if (-not $NssmPath) {
    Write-Fail "NSSM not found after install. Restart this terminal and re-run install.ps1"
}
Write-Ok "NSSM at $NssmPath"

# -- Register service -----------------------------------------------------

Write-Step "Registering service '$ServiceName'..."

$ErrorActionPreference = "Continue"
& $NssmPath status $ServiceName 2>$null | Out-Null
$serviceExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"

if ($serviceExists) {
    Write-Warn "Service '$ServiceName' already exists - removing and re-registering"
    $ErrorActionPreference = "Continue"
    & $NssmPath stop $ServiceName 2>$null | Out-Null
    & $NssmPath remove $ServiceName confirm 2>$null | Out-Null
    $ErrorActionPreference = "Stop"
    Start-Sleep -Seconds 2
}

$LogDir = "$RepoRoot\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$UvArgs = "run --project `"$RepoRoot`" python `"$RepoRoot\goal_detector.py`" --server $ServerUrl --net-id $NetId"

& $NssmPath install    $ServiceName $UvPath $UvArgs
& $NssmPath set        $ServiceName AppDirectory               $RepoRoot
& $NssmPath set        $ServiceName AppExit                    Default Restart
& $NssmPath set        $ServiceName AppRestartDelay            3000
& $NssmPath set        $ServiceName AppStdout                  "$LogDir\stdout.log"
& $NssmPath set        $ServiceName AppStderr                  "$LogDir\stderr.log"
& $NssmPath set        $ServiceName AppStdoutCreationDisposition 4
& $NssmPath set        $ServiceName AppStderrCreationDisposition 4
& $NssmPath set        $ServiceName AppRotateFiles             1
& $NssmPath set        $ServiceName AppRotateBytes             10485760
& $NssmPath set        $ServiceName Description                "HHOF Goalie Simulator - goal detector ($NetId)"
& $NssmPath set        $ServiceName Start                      SERVICE_AUTO_START

Write-Ok "Service registered"

# -- Start service (only if calibrated) -----------------------------------

Write-Step "Checking calibration..."

$Calibrated = $false
$ConfigPath = "$RepoRoot\config.json"
if (Test-Path $ConfigPath) {
    try {
        $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        if ($cfg.goal_line -and $cfg.net_roi -and $cfg.approach_roi) {
            $Calibrated = $true
        }
    } catch {}
}

if ($Calibrated) {
    Write-Ok "Calibration found - starting service"
    & $NssmPath start $ServiceName
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Service did not start cleanly. Check with: nssm status $ServiceName"
    } else {
        Write-Ok "Service started"
    }
} else {
    Write-Warn "No calibration data found - service registered but NOT started"
    Write-Warn "Run calibration first, then start the service manually:"
    Write-Warn "  uv run goal-detector --calibrate"
    Write-Warn "  nssm start $ServiceName"
}

# -- Summary --------------------------------------------------------------

Write-Host "`n----------------------------------------" -ForegroundColor Green
Write-Host " Install complete" -ForegroundColor Green
Write-Host "----------------------------------------" -ForegroundColor Green
Write-Host " Service  : $ServiceName"
Write-Host " Net ID   : $NetId"
Write-Host " Server   : $ServerUrl"
Write-Host " Logs     : $LogDir"
if (-not $Calibrated) {
    Write-Host ""
    Write-Host " NEXT STEP - calibrate before starting:" -ForegroundColor Yellow
    Write-Host "   uv run goal-detector --calibrate" -ForegroundColor Yellow
    Write-Host "   nssm start $ServiceName" -ForegroundColor Yellow
}
Write-Host ""
Write-Host " Useful commands:"
Write-Host "   nssm status $ServiceName"
Write-Host "   nssm restart $ServiceName"
Write-Host "   nssm stop $ServiceName"
Write-Host "   nssm remove $ServiceName confirm"
Write-Host "   Get-Content `"$LogDir\stdout.log`" -Tail 50"
Write-Host "----------------------------------------`n" -ForegroundColor Green
