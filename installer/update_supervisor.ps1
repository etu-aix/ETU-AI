param(
  [int]$ServerPid,
  [int]$LauncherPid,
  [int]$CompanionPid,
  [string]$Installer,
  [string]$AppLauncher,
  [string]$AppCompanion,
  [string]$CompanionPidFile,
  [string]$SupervisorFile,
  [string]$ExpectedSha256,
  [long]$ExpectedSize,
  [string]$Version
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
$installDir = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($AppLauncher))
$statusFile = Join-Path $installDir 'storage\app-update-status.json'
function Write-UpdateStatus([string]$status, [string]$message) {
  $statusDir = Split-Path -Parent $statusFile
  [IO.Directory]::CreateDirectory($statusDir) | Out-Null
  $temporaryStatus = "$statusFile.tmp"
  @{ status = $status; version = $Version; message = $message } |
    ConvertTo-Json -Compress | Set-Content -LiteralPath $temporaryStatus -Encoding UTF8
  Move-Item -LiteralPath $temporaryStatus -Destination $statusFile -Force
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
    $expectedCompanion = [IO.Path]::GetFullPath($AppCompanion)
    $isCompanion = $false
    if ($candidate -and $candidate.Path) {
      $isCompanion = [string]::Equals([IO.Path]::GetFullPath([string]$candidate.Path), $expectedCompanion, [StringComparison]::OrdinalIgnoreCase)
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
    Write-UpdateStatus 'installing' 'Mise à jour en cours…'
    $setup = Start-Process -FilePath $installerPath -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=`"$installDir`"") -WindowStyle Hidden -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "Installer exited with code $($setup.ExitCode)." }
    Write-UpdateStatus 'completed' 'Mise à jour terminée.'
  } finally {
    if ($packageStream) { $packageStream.Dispose() }
    $sha256.Dispose()
  }
} catch {
  Write-UpdateStatus 'error' "Échec de la mise à jour : $($_.Exception.Message)"
  throw
} finally {
  # Relaunch the existing installation after success, cancellation, or failure.
  Remove-Item -LiteralPath $companionPidFilePath -Force -ErrorAction SilentlyContinue
  try {
    if (Test-Path -LiteralPath $AppLauncher) {
      Start-Process -FilePath $AppLauncher -ArgumentList '--downloads' -WorkingDirectory $installDir -WindowStyle Hidden
    }
  } catch { }
  Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $SupervisorFile -Force -ErrorAction SilentlyContinue
}
