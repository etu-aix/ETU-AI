$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  $python = (Get-Command python -ErrorAction Stop).Source
}

Push-Location $root
try {
  & $python -m PyInstaller --onefile --noconsole --icon (Join-Path $root 'assets\etu-ai_logo.ico') `
    --name ETU-AI-Companion --collect-all webview --collect-submodules pynput `
    --distpath $root --workpath (Join-Path $root '.tmp\pyinstaller-companion') `
    --specpath (Join-Path $root '.tmp\pyinstaller-companion') companion.py
  if ($LASTEXITCODE -ne 0) { throw 'Compilation du compagnon impossible. Vérifiez PyInstaller et requirements-desktop.txt.' }
} finally {
  Pop-Location
}
