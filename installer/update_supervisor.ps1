param(
  [int]$ServerPid,
  [int]$LauncherPid,
  [int]$CompanionPid,
  [string]$Installer,
  [string]$AppLauncher,
  [string]$AppPython,
  [string]$CompanionPidFile,
  [string]$SupervisorFile,
  [string]$ExpectedSha256,
  [long]$ExpectedSize
)

$ErrorActionPreference = 'Stop'
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$installerPath = [IO.Path]::GetFullPath($Installer)
$companionPidFilePath = [IO.Path]::GetFullPath($CompanionPidFile)
if (-not $tempRoot.EndsWith([IO.Path]::DirectorySeparatorChar)) { $tempRoot += [IO.Path]::DirectorySeparatorChar }
if (-not $installerPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
    [IO.Path]::GetFileName($installerPath) -notlike 'etu-app-installer-*.exe' -or
    -not $companionPidFilePath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $ExpectedSha256 -notmatch '^[0-9a-f]{64}$' -or $ExpectedSize -le 0) {
  exit 2
}

$deadline = [DateTime]::UtcNow.AddSeconds(90)
do {
  $alive = $false
  foreach ($processId in @($ServerPid, $LauncherPid)) {
    if ($processId -gt 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { $alive = $true }
  }
  if (-not $alive) { break }
  Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)

try {
  if ($alive) { throw 'ETU-AI did not shut down before the installer timeout.' }
  if ($CompanionPid -gt 0) {
    $candidate = Get-Process -Id $CompanionPid -ErrorAction SilentlyContinue
    $expectedPython = [IO.Path]::GetFullPath($AppPython)
    $isCompanion = $false
    if ($candidate -and $candidate.Path) {
      $isCompanion = [string]::Equals([IO.Path]::GetFullPath([string]$candidate.Path), $expectedPython, [StringComparison]::OrdinalIgnoreCase)
    }
    if ($isCompanion) {
      Stop-Process -Id $CompanionPid -Force -ErrorAction SilentlyContinue
      $companionDeadline = [DateTime]::UtcNow.AddSeconds(10)
      while ((Get-Process -Id $CompanionPid -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $companionDeadline) {
        Start-Sleep -Milliseconds 200
      }
      if (Get-Process -Id $CompanionPid -ErrorAction SilentlyContinue) { throw 'The ETU-AI companion did not close before the installer.' }
    }
  }
  $sha256 = [Security.Cryptography.SHA256]::Create()
  $packageStream = $null
  try {
    # Deny concurrent writes/deletes from the final hash check until setup exits.
    $packageStream = [IO.File]::Open($installerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    if ($packageStream.Length -ne $ExpectedSize) { throw 'Installer size changed after authentication.' }
    $actualSha256 = [BitConverter]::ToString($sha256.ComputeHash($packageStream)).Replace('-', '').ToLowerInvariant()
    if ($actualSha256 -ne $ExpectedSha256) { throw 'Installer hash changed after authentication.' }
    $setup = Start-Process -FilePath $installerPath -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "Installer exited with code $($setup.ExitCode)." }
  } finally {
    if ($packageStream) { $packageStream.Dispose() }
    $sha256.Dispose()
  }
} finally {
  # Relaunch the existing installation after success, cancellation, or failure.
  Remove-Item -LiteralPath $companionPidFilePath -Force -ErrorAction SilentlyContinue
  try {
    if (Test-Path -LiteralPath $AppLauncher) {
      Start-Process -FilePath $AppLauncher -WorkingDirectory (Split-Path -Parent $AppLauncher)
    }
  } catch { }
  Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $SupervisorFile -Force -ErrorAction SilentlyContinue
}
