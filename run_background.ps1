# Keeps the Home API Dashboard running in the background: launches it hidden
# and relaunches it if it ever exits/crashes. Output is logged for troubleshooting
# (pythonw.exe would swallow startup errors silently, so this uses python.exe
# with its window hidden and stdout/stderr redirected instead).
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$logPath = Join-Path $scriptDir "background.log"
$outLog = Join-Path $scriptDir "background-out.log"
$errLog = Join-Path $scriptDir "background-err.log"

while ($true) {
    "[$(Get-Date -Format o)] starting app.py" | Out-File -FilePath $logPath -Append -Encoding utf8
    Start-Process -FilePath "$scriptDir\.venv\Scripts\python.exe" -ArgumentList "app.py" `
        -WorkingDirectory $scriptDir -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -Wait
    "[$(Get-Date -Format o)] app.py exited, restarting in 5s" | Out-File -FilePath $logPath -Append -Encoding utf8
    Start-Sleep -Seconds 5
}
