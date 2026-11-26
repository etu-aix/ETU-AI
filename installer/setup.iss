[Setup]
AppId={{D9B318E1-B2EF-4F6D-9543-A4291F0924C8}
AppName=ETU-AI
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\ETU-AI
DefaultGroupName=ETU-AI
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=ETU-AI-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=ETU-AI

[Files]
Source: "..\release-payload\*"; DestDir: "{app}"; Excludes: "ai-server\config.json"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\release-payload\ai-server\config.json"; DestDir: "{app}\ai-server"; Flags: onlyifdoesntexist

[Icons]
Name: "{autodesktop}\ETU-AI"; Filename: "{app}\ETU-AI.exe"; WorkingDir: "{app}"
Name: "{group}\ETU-AI"; Filename: "{app}\ETU-AI.exe"; WorkingDir: "{app}"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ETU-AI"; Flags: uninsdeletevalue

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if not Exec(ExpandConstant('{app}\python\python.exe'),
      '"' + ExpandConstant('{app}\installer\prepare.py') + '"',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('Impossible de préparer ETU-AI.')
    else if ResultCode <> 0 then
      RaiseException('La création de l’environnement ETU-AI a échoué. Vérifiez votre connexion Internet et relancez le programme d’installation.');
  end;
end;
