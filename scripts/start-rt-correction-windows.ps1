param(
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
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -B app.py --host 127.0.0.1 --port $Port --rt-correction
        exit $LASTEXITCODE
    }
}
if (-not $PythonPath) {
    throw "Python was not found. Install Python 3.10+ or pass -PythonPath C:\path\to\python.exe."
}

Write-Host "Starting MS-DIAL RT Correction Review..."
Write-Host "URL: http://127.0.0.1:$Port/rt-correction"
Write-Host "Keep this terminal open while the app is in use."
& $PythonPath -B app.py --host 127.0.0.1 --port $Port --rt-correction
