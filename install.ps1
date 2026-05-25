# HHOF Goalie Simulator — Goal Detector Windows Service Installer

$ErrorActionPreference = "Stop"

# ── Self-elevate if not running as admin ─────────────────────────────

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Relaunching as Administrator..."
    Start-Process powershell "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# ── Helpers ──────────────────────────────────────────────────────────

function Write-Step { param($msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "   ERROR: $msg" -ForegroundColor Red; exit 1 }

# ── Verify we're in the repo root ────────────────────────────────────

$RepoRoot = $PSScriptRoot
if (-not (Test-Path "$RepoRoot\goal_detector.py")) {
    Write-Fail "Run this script from the goal-detection repo root"
}

# ── Prompts ──────────────────────────────────────────────────────────

Write-Host "`nHHOF Goal Detector — Service Installer" -ForegroundColor Green
Write-Host "Press Enter to accept the default shown in brackets.`n"

$ServerUrl = Read-Host "WebSocket server URL  [ws://localhost:8765]"
if (-not $ServerUrl) { $ServerUrl = "ws://localhost:8765" }

$NetId = Read-Host "Net ID                [net_1]"
if (-not $NetId) { $NetId = "net_1" }

$ServiceName = Read-Host "Service name          [hhof-goal-detector]"
if (-not $ServiceName) { $ServiceName = "hhof-goal-detector" }

# ── Install uv ───────────────────────────────────────────────────────

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
    Write-Fail "uv installation failed — install manually from https://docs.astral.sh/uv"
}
Write-Ok "uv at $UvPath"

# ── Install Python dependencies ───────────────────────────────────────

Write-Step "Installing Python dependencies..."
& $UvPath sync --project $RepoRoot
if ($LASTEXITCODE -ne 0) { Write-Fail "uv sync failed" }
Write-Ok "Core dependencies ready (numpy, opencv-python, websockets)"

# ── Install arena_api (Lucid Vision) ─────────────────────────────────

Write-Step "Checking arena_api (Lucid Vision GigE)..."

$ArenaApiInstalled = & $UvPath run --project $RepoRoot python -c "import arena_api" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Ok "arena_api already installed"
} else {
    # Search for the wheel bundled with the Arena SDK
    $SdkRoot = "C:\Program Files\Lucid Vision Labs\Arena SDK"
    $Wheel = Get-ChildItem -Path $SdkRoot -Recurse -Filter "arena_api-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1

    if ($Wheel) {
        Write-Host "   Found wheel: $($Wheel.FullName)"
        & $UvPath pip install --project $RepoRoot $Wheel.FullName
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "arena_api installed from SDK wheel"
        } else {
            Write-Warn "arena_api wheel install failed — GigE camera will not work"
        }
    } else {
        Write-Warn "arena_api wheel not found under '$SdkRoot'"
        Write-Warn "GigE camera will not work. To fix, install the Arena SDK then re-run this script,"
        Write-Warn "or run: pip install `"<SDK path>\python\arena_api-*.whl`""
    }
}

# ── Install NSSM ─────────────────────────────────────────────────────

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

# ── Register service ──────────────────────────────────────────────────

Write-Step "Registering service '$ServiceName'..."

$existing = & $NssmPath status $ServiceName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Warn "Service '$ServiceName' already exists — removing and re-registering"
    & $NssmPath stop $ServiceName 2>&1 | Out-Null
    & $NssmPath remove $ServiceName confirm | Out-Null
}

$LogDir = "$RepoRoot\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$UvArgs = "run --project `"$RepoRoot`" goal-detector --server $ServerUrl --net-id $NetId"

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
& $NssmPath set        $ServiceName Description                "HHOF Goalie Simulator — goal detector ($NetId)"
& $NssmPath set        $ServiceName Start                      SERVICE_AUTO_START

Write-Ok "Service registered"

# ── Start service ─────────────────────────────────────────────────────

Write-Step "Starting service..."
& $NssmPath start $ServiceName

if ($LASTEXITCODE -ne 0) {
    Write-Warn "Service did not start cleanly. Check with: nssm status $ServiceName"
} else {
    Write-Ok "Service started"
}

# ── Summary ───────────────────────────────────────────────────────────

Write-Host "`n────────────────────────────────────────" -ForegroundColor Green
Write-Host " Install complete" -ForegroundColor Green
Write-Host "────────────────────────────────────────" -ForegroundColor Green
Write-Host " Service  : $ServiceName"
Write-Host " Net ID   : $NetId"
Write-Host " Server   : $ServerUrl"
Write-Host " Logs     : $LogDir"
Write-Host ""
Write-Host " Useful commands:"
Write-Host "   nssm status $ServiceName"
Write-Host "   nssm restart $ServiceName"
Write-Host "   nssm stop $ServiceName"
Write-Host "   nssm remove $ServiceName confirm"
Write-Host "   Get-Content `"$LogDir\stdout.log`" -Tail 50"
Write-Host "────────────────────────────────────────`n" -ForegroundColor Green
