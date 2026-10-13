; Inno Setup script. Build with:  iscc /DAppVersion=1.2.3 installer\BoseSoundbarRemote.iss
; Per-user install (no administrator prompt). Same AppId on every release, so a new installer upgrades in place.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Bose Soundbar Remote"
#define AppExe "BoseSoundbarRemote.exe"

[Setup]
AppId={{B7C2F1A4-5D3E-4C8B-9A61-2E4F7D8C3A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Bose Soundbar Remote contributors
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=BoseSoundbarRemote-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"
Name: "startup"; Description: "Start Bose Soundbar Remote (minimized) when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\BoseSoundbarRemote\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

; The shortcut names match the ones the app's own Settings page creates, so the two never duplicate each other.
[Icons]
Name: "{userprograms}\Bose Soundbar"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\Bose Soundbar"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\Bose Soundbar"; Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Tasks: startup

[Run]
; Normal install: offer to open it. Silent (self-update) install: relaunch it automatically.
Filename: "{app}\{#AppExe}"; Description: "Open Bose Soundbar Remote"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
Type: files; Name: "{userdesktop}\Bose Soundbar.lnk"
Type: files; Name: "{userprograms}\Bose Soundbar.lnk"
Type: files; Name: "{userstartup}\Bose Soundbar.lnk"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if not UninstallSilent then
      if MsgBox('Also delete your saved Bose login and settings?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ExpandConstant('{userappdata}\BoseSoundbarRemote'), True, True, True);
end;
