Write-Host "Launching monitor on port 8010..."
$python = "C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

# Pass arguments as an ARRAY (not a single string) so Start-Process passes them
# verbatim without going through the .py file association (which would spawn uv).
$proc = Start-Process -FilePath $python `
    -ArgumentList @("-u", "monitor.py", "--web", "--port", "8010") `
    -WorkingDirectory "C:\TP\URLFinder" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "C:\TP\URLFinder\batch_run.monitor.log" `
    -RedirectStandardError "C:\TP\URLFinder\batch_run.monitor.err.log" `
    -PassThru
Write-Host "Launched monitor PID $($proc.Id)"
