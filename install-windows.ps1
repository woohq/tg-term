# tg-term Windows installer
# Run: powershell -ExecutionPolicy Bypass -File install-windows.ps1

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir "tg-term.lnk"
$batPath = Join-Path $scriptDir "start-tg-term.bat"

# Check prerequisites
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "ERROR: Python not found. Install Python 3 and add to PATH." -ForegroundColor Red
    exit 1
}

$wezterm = Get-Command wezterm -ErrorAction SilentlyContinue
if (-not $wezterm) {
    Write-Host "ERROR: WezTerm not found. Install WezTerm and add to PATH." -ForegroundColor Red
    exit 1
}

# Check requests module
python -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing requests..." -ForegroundColor Yellow
    pip install requests
}

# Check .env exists
$envFile = Join-Path $scriptDir ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "WARNING: No .env file found. Copy .env.example to .env and fill in your bot token." -ForegroundColor Yellow
    Write-Host "  cp .env.example .env" -ForegroundColor Yellow
}

# Create startup shortcut
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $batPath
$shortcut.WorkingDirectory = $scriptDir
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = "tg-term: Telegram to WezTerm bridge"
$shortcut.Save()

Write-Host "Installed! tg-term will auto-start on login." -ForegroundColor Green
Write-Host "  Shortcut: $shortcutPath" -ForegroundColor Cyan
Write-Host "  To start now: .\start-tg-term.bat" -ForegroundColor Cyan
