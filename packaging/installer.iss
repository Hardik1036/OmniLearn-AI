; OmniLearn AI — Windows Desktop System App Installer (Inno Setup 6)
; Build first with: powershell -ExecutionPolicy Bypass -File packaging\pyinstaller_build.ps1

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "OmniLearn AI"
#define MyAppPublisher "OmniLearn AI Project"
#define MyAppURL "https://github.com/omnilearn-ai/omnilearn"
#define MyAppExeName "OmniLearn.exe"
#define MyAppAssocExt ".pdf,.xlsx,.pptx,.csv,.txt,.md,.mp4"

[Setup]
AppId={{8E9B3B2D-7B19-4C46-9D8A-1F73F1E5A4C2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\OmniLearn AI
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=OmniLearn-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "quicklaunchicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "{cm:AdditionalIcons}"
Name: "shellassoc"; Description: "Add 'Open with OmniLearn AI' context menu entry for study documents (.pdf, .pptx, .xlsx, .csv, .md, .mp4)"; GroupDescription: "File Integration"; Flags: unchecked

[Files]
Source: "..\dist\OmniLearn\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} (Headless Server Mode)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--headless"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional per-user shell context menu ("Open with OmniLearn AI")
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pptx\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pptx\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.xlsx\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.xlsx\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.csv\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.csv\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.md\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.md\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\OmniLearnAI"; ValueType: string; ValueName: ""; ValueData: "Open with OmniLearn AI"; Tasks: shellassoc; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.mp4\shell\OmniLearnAI\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: shellassoc

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function HasWebView2Runtime(): Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not HasWebView2Runtime() then
  begin
    if MsgBox(
      'Microsoft Edge WebView2 Runtime was not detected on this system.'#13#10 +
      'OmniLearn AI requires Edge WebView2 Runtime for its desktop window (already included on Windows 11 and updated Windows 10).' + #13#10#13#10 +
      'Headless browser mode (--headless) still works without it. Continue installation anyway?',
      mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
    end;
  end;
end;
