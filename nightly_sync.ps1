# Runs a few hours after nightly_start.ps1 (gives MinusPod time to finish
# processing). Merges multi-part episodes and syncs the result to your NAS.
$RepoDir = $PSScriptRoot
$LogFile = Join-Path $RepoDir "nightly.log"
function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -Append -FilePath $LogFile -Encoding utf8
}

Log "===== MERGE + SYNC ====="
Set-Location $RepoDir

# ffmpeg/ffprobe must be on PATH. Adjust this to wherever yours is installed
# (or delete this line entirely if ffmpeg is already on your system PATH).
# $env:PATH += ";C:\path\to\your\ffmpeg\bin"

python merge_multipart.py 2>&1 | Out-File -Append -FilePath $LogFile -Encoding utf8
python sync_to_nas.py 2>&1 | Out-File -Append -FilePath $LogFile -Encoding utf8

Log "===== DONE ====="
