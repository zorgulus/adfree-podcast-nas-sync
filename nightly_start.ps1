# Runs at 23:00 (or whenever you schedule it): starts Docker Desktop + MinusPod
# if needed, then triggers a feed refresh to pick up newly published episodes.
$RepoDir = $PSScriptRoot
$LogFile = Join-Path $RepoDir "nightly.log"
function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -Append -FilePath $LogFile -Encoding utf8
}

Log "===== NIGHTLY START ====="

# Start Docker Desktop if it isn't already running.
$dockerRunning = docker ps 2>$null
if (-not $?) {
    Log "Docker Desktop not running, starting it..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $tries = 0
    while ($tries -lt 30) {
        Start-Sleep -Seconds 10
        docker ps 2>$null | Out-Null
        if ($?) { break }
        $tries++
    }
}

if (-not $?) {
    Log "FAILED: Docker did not start in time"
    exit 1
}
Log "Docker is up"

# Make sure MinusPod is running (recreates only if needed, leaves it alone if already up).
Set-Location $RepoDir
docker compose -f docker-compose.cpu.yml up -d 2>&1 | Out-File -Append -FilePath $LogFile -Encoding utf8

Start-Sleep -Seconds 10

# Trigger a refresh of all feeds so new episodes get picked up.
try {
    Invoke-RestMethod -Uri "http://localhost:8000/api/v1/feeds/refresh" -Method Post -TimeoutSec 30 | Out-Null
    Log "Feed refresh triggered"
} catch {
    Log "WARNING: feed refresh failed - $($_.Exception.Message)"
}

Log "Nightly start done, processing continues in the background"
