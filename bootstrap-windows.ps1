# =============================================================================
# Windows PC Bootstrap Script
# Sets up: folder structure, SSH keys, all repos, gl, tg-term auto-start
# =============================================================================
# Run: powershell -ExecutionPolicy Bypass -File bootstrap-windows.ps1
# =============================================================================

param(
    [string]$CsRoot = "$env:USERPROFILE\cs"
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  FAIL: $msg" -ForegroundColor Red }

# ─── Prerequisites ──────────────────────────────────────────────────────────

Step "Checking prerequisites"

$prereqs = @("git", "python", "ssh")
foreach ($cmd in $prereqs) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        Ok "$cmd found"
    } else {
        Fail "$cmd not found. Install it and re-run."
        exit 1
    }
}

# Check WezTerm (optional at clone time, required for tg-term)
if (Get-Command wezterm -ErrorAction SilentlyContinue) {
    Ok "wezterm found"
} else {
    Warn "WezTerm not found. Install it before running tg-term."
}

# Install requests
python -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Step "Installing Python requests"
    pip install requests
}

# ─── SSH Key Setup ──────────────────────────────────────────────────────────

Step "SSH key setup"

$sshDir = "$env:USERPROFILE\.ssh"
if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir | Out-Null }

$woohqKey = "$sshDir\id_ed25519_woohq"
if (Test-Path $woohqKey) {
    Ok "woohq SSH key exists"
} else {
    Warn "No woohq SSH key found at $woohqKey"
    Write-Host "  Option 1: Copy from your Mac:" -ForegroundColor White
    Write-Host "    scp mac:~/.ssh/id_ed25519_woohq* $sshDir\" -ForegroundColor Gray
    Write-Host "  Option 2: Generate new key and add to GitHub:" -ForegroundColor White
    Write-Host "    ssh-keygen -t ed25519 -f $woohqKey" -ForegroundColor Gray
    Write-Host ""
    $proceed = Read-Host "Press Enter once your SSH key is ready (or 'skip' to continue without it)"
    if ($proceed -eq "skip") {
        Warn "Skipping SSH setup — woohq repos will use HTTPS fallback"
    }
}

# Write SSH config
$sshConfig = "$sshDir\config"
$woohqBlock = @"

Host github.com-woohq
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_woohq
"@

if (Test-Path $sshConfig) {
    $existing = Get-Content $sshConfig -Raw
    if ($existing -notmatch "github.com-woohq") {
        Add-Content $sshConfig $woohqBlock
        Ok "Added github.com-woohq to SSH config"
    } else {
        Ok "SSH config already has github.com-woohq"
    }
} else {
    Set-Content $sshConfig $woohqBlock
    Ok "Created SSH config with github.com-woohq"
}

# ─── Folder Structure ──────────────────────────────────────────────────────

Step "Creating folder structure"

$dirs = @("$CsRoot\pr", "$CsRoot\wk", "$CsRoot\scl", "$CsRoot\other\archive")
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
}
Ok "cs/{pr,wk,scl,other/archive}"

# ─── Clone Repos ────────────────────────────────────────────────────────────

Step "Cloning repositories"

# Format: [destination_subfolder, repo_url]
$repos = @(
    # ── personal (pr/) ──
    @("pr\tg-term",           "git@github.com-woohq:woohq/tg-term.git"),
    @("pr\gl",                "git@github.com-woohq:woohq/gl.git"),
    @("pr\atelier-mcp",      "git@github.com-woohq:woohq/atelier-mcp.git"),
    @("pr\frette",           "git@github.com-woohq:woohq/frette.git"),
    @("pr\wren-s",           "git@github.com-woohq:woohq/wren-s.git"),
    @("pr\elmo",             "git@github.com-woohq:woohq/elmo.git"),
    @("pr\godette-mcp",      "git@github.com-woohq:woohq/godette.git"),
    @("pr\study-system",     "git@github.com-woohq:woohq/study-system.git"),
    @("pr\minichord",        "git@github.com-woohq:woohq/minichord.git"),

    # ── work (wk/) ──
    @("wk\SCOUTV2",          "git@github.com:Vast-Solutions/Scout.git"),
    @("wk\SCOUT-MONOREPO",   "git@github.com:Vast-Solutions/Scout.git"),

    # ── school (scl/) ──
    @("scl\CS480",            "git@github.com-woohq:jmu-cs480-2026a/cs480-quanhx.git")
)

foreach ($r in $repos) {
    $dest = Join-Path $CsRoot $r[0]
    $url = $r[1]
    if (Test-Path $dest) {
        Ok "$($r[0]) already exists, skipping"
    } else {
        Write-Host "  Cloning $($r[0])..." -ForegroundColor White
        try {
            git clone $url $dest 2>&1 | Out-Null
            Ok $r[0]
        } catch {
            Warn "Failed to clone $($r[0]): $_"
        }
    }
}

Write-Host ""
Warn "Repos without remotes (copy manually from Mac if needed):"
Write-Host "  pr/agent, pr/qualify, pr/resume, pr/leetcode, pr/MoneyPrinterV2-main, pr/vector-base" -ForegroundColor Gray
Write-Host "  scl/CS412, scl/CS430, scl/M268" -ForegroundColor Gray

# ─── Install gl ─────────────────────────────────────────────────────────────

Step "Installing gl"

$glDir = Join-Path $CsRoot "pr\gl"
if (Test-Path $glDir) {
    $glInstaller = Join-Path $glDir "install-windows.ps1"
    if (Test-Path $glInstaller) {
        & $glInstaller
    } else {
        Warn "gl\install-windows.ps1 not found. Install gl manually."
    }
} else {
    Warn "gl not cloned. Skipping."
}

# ─── gl sync remote for SCOUTV2 ────────────────────────────────────────────

Step "Setting up gl on SCOUTV2"

$scoutDir = Join-Path $CsRoot "wk\SCOUTV2"
if (Test-Path $scoutDir) {
    Push-Location $scoutDir
    try {
        gl init 2>$null
        Ok "gl init done"
    } catch {
        Warn "gl init failed (gl may not be in PATH yet). Run manually: cd $scoutDir && gl init"
    }

    # Add sync remote — using a private woohq repo
    # User needs to create this repo first: gh repo create woohq/scout-sync --private
    try {
        gl remote add sync "git@github.com-woohq:woohq/scout-sync.git" 2>$null
        Ok "sync remote configured"
    } catch {
        Warn "Could not set sync remote. Run manually after creating woohq/scout-sync"
    }
    Pop-Location
} else {
    Warn "SCOUTV2 not cloned. Skipping."
}

# ─── tg-term setup ─────────────────────────────────────────────────────────

Step "Setting up tg-term"

$tgDir = Join-Path $CsRoot "pr\tg-term"
if (Test-Path $tgDir) {
    $envFile = Join-Path $tgDir ".env"
    if (-not (Test-Path $envFile)) {
        Copy-Item (Join-Path $tgDir ".env.example") $envFile
        Warn ".env created from template. Edit it with your bot token and user ID:"
        Write-Host "    notepad $envFile" -ForegroundColor Gray
    } else {
        Ok ".env already exists"
    }

    # Install auto-start
    $installer = Join-Path $tgDir "install-windows.ps1"
    if (Test-Path $installer) {
        & $installer
    }
} else {
    Warn "tg-term not cloned. Skipping."
}

# ─── Summary ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Setup complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Remaining manual steps:" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. SSH key: Make sure ~/.ssh/id_ed25519_woohq is set up" -ForegroundColor White
Write-Host "   (copy from Mac or generate new + add to GitHub)" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Create sync repo (run once, from either machine):" -ForegroundColor White
Write-Host "   gh auth switch --user woohq" -ForegroundColor Gray
Write-Host "   gh repo create woohq/scout-sync --private" -ForegroundColor Gray
Write-Host "   gh auth switch --user VSHenryQ" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Mac-side gl sync setup (run on your Mac):" -ForegroundColor White
Write-Host "   cd ~/cs/wk/SCOUTV2" -ForegroundColor Gray
Write-Host "   gl remote add sync git@github.com-woohq:woohq/scout-sync.git" -ForegroundColor Gray
Write-Host ""
Write-Host "4. Edit tg-term .env with your bot token:" -ForegroundColor White
Write-Host "   notepad $CsRoot\pr\tg-term\.env" -ForegroundColor Gray
Write-Host ""
Write-Host "5. Start tg-term:" -ForegroundColor White
Write-Host "   $CsRoot\pr\tg-term\start-tg-term.bat" -ForegroundColor Gray
Write-Host "   (will auto-start on login going forward)" -ForegroundColor Gray
Write-Host ""
