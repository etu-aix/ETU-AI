param(
  [Parameter(Mandatory = $true)][string]$SetupPath,
  [Parameter(Mandatory = $true)][string]$ExpectedVersion
)

$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'The installer smoke test must run as a standard, non-administrator user.'
}

$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\ETU-AI-standard-user-smoke'
$arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="' + $installRoot + '"'
$install = Start-Process -FilePath $SetupPath -ArgumentList $arguments -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "Standard-user setup install failed: $($install.ExitCode)" }

$launcherPath = Join-Path $installRoot 'ETU-AI.exe'
$pythonPath = Join-Path $installRoot 'python\python.exe'
$versionPath = Join-Path $installRoot 'VERSION'
if (-not (Test-Path $launcherPath) -or -not (Test-Path $pythonPath)) {
  throw 'The standard-user setup did not install the launcher and bundled Python.'
}
if ((Get-Content -Raw $versionPath).Trim() -ne $ExpectedVersion) {
  throw 'The standard-user setup installed an unexpected application version.'
}

$env:PATH = Join-Path $env:SystemRoot 'System32'
$launcher = $null
$serverPid = 0
try {
  $launcher = Start-Process -FilePath $launcherPath -ArgumentList @('--mode', 'local') -WorkingDirectory $installRoot -WindowStyle Hidden -PassThru
  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  $ready = $false
  do {
    try {
      $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8900/api/status' -TimeoutSec 3
      if ($status.status -eq 'online') { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
  } while ([DateTime]::UtcNow -lt $deadline)
  if (-not $ready) { throw 'ETU-AI.exe did not launch for a standard Windows user without network access.' }
  $launchMode = Invoke-RestMethod -Uri 'http://127.0.0.1:8900/api/launch-mode' -TimeoutSec 3
  $serverPid = [int]$launchMode.pid
  Invoke-RestMethod -Uri 'http://127.0.0.1:8900/api/power/shutdown' -Method Post -Headers @{ Origin = 'http://127.0.0.1:8900' } -TimeoutSec 5 | Out-Null
  Write-Output 'Standard Windows user installed ETU-AI and launched the bundled app offline.'
} finally {
  if ($serverPid -le 0) {
    try { $serverPid = [int](Invoke-RestMethod -Uri 'http://127.0.0.1:8900/api/launch-mode' -TimeoutSec 2).pid } catch { }
  }
  if ($serverPid -gt 0) {
    $serverProcess = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    if ($serverProcess -and -not $serverProcess.WaitForExit(10000)) { Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue }
  }
  if ($launcher -and (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) {
    try { & taskkill.exe /PID $launcher.Id /T /F 2>$null | Out-Null } catch { }
  }
}
