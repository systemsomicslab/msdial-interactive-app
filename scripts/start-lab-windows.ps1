param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8765,
    [string]$ConsolePath = $env:MSDIAL_CONSOLE_PATH,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$AppRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $AppRoot

if ($ConsolePath) {
    $env:MSDIAL_CONSOLE_PATH = $ConsolePath
}

if (-not $PythonPath) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $PythonPath = $pythonCommand.Source
    }
}
if (-not $PythonPath) {
    throw "Python was not found. Install Python 3.10+ or pass -PythonPath C:\path\to\python.exe."
}

Write-Host "Starting MS-DIAL Interactive for lab-internal access..."
Write-Host "App root: $AppRoot"
Write-Host "Host: $HostAddress"
Write-Host "Port: $Port"
if ($env:MSDIAL_CONSOLE_PATH) {
    Write-Host "MS-DIAL Console: $env:MSDIAL_CONSOLE_PATH"
} else {
    Write-Host "MS-DIAL Console: auto-detect or set later in the UI"
}
Write-Host "Python: $PythonPath"
Write-Host "Keep this terminal open while the app is in use."

& $PythonPath -B app.py --host $HostAddress --port $Port --no-browser
