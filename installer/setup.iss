[Setup]
AppId={{D9B318E1-B2EF-4F6D-9543-A4291F0924C8}
AppName=ETU-AI
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\ETU-AI
DefaultGroupName=ETU-AI
UsePreviousTasks=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=ETU-AI-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=ETU-AI
SetupIconFile=..\assets\etu-ai_logo.ico
UninstallDisplayIcon={app}\ETU-AI.exe

[Files]
Source: "..\release-payload\*"; DestDir: "{app}"; Excludes: "ai-server\config.json,tools\windows_standard_user_smoke.ps1,tools\build_companion.ps1"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\release-payload\ai-server\config.json"; DestDir: "{app}\ai-server"; Flags: onlyifdoesntexist

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; GroupDescription: "Raccourcis :"

[Icons]
Name: "{autodesktop}\ETU-AI"; Filename: "{app}\ETU-AI.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ETU-AI.exe"; Tasks: desktopicon
Name: "{group}\ETU-AI"; Filename: "{app}\ETU-AI.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\ETU-AI.exe"; Description: "Lancer ETU-AI"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ETU-AI"; Flags: uninsdeletevalue
