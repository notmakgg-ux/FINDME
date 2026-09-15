Write-Host "Launching pipeline..."
$python = "C:\Users\isourcing\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
$script = "crawler\run_pipeline_cli.py"
$locations = @(
    "Cleveland, Ohio , USA",
    "Charlotte, North Carolina, USA",
    "Providence, Rhode Island , USA",
    "Kansas City, Missouri, USA",
    "Dallas,Texas,USA"
)
# Build ONE argument string. Each location is wrapped in double quotes so
# the child's CommandLineToArgvW parses it as a single arg (commas are safe
# inside quotes).
$argStr = "-u `"$script`" --min-results 500"
foreach ($loc in $locations) {
    $argStr += " --location `"$loc`""
}
Write-Host "ArgStr: $argStr"

$proc = Start-Process -FilePath $python -ArgumentList $argStr `
    -WorkingDirectory "C:\TP\URLFinder" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "C:\TP\URLFinder\batch_run.log" `
    -RedirectStandardError "C:\TP\URLFinder\batch_run.err.log" `
    -PassThru
Write-Host "Launched PID $($proc.Id)"
