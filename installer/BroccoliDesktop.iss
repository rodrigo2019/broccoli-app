#define MyAppId "{{7C87D246-7E61-48A2-A0C4-57B3C56180B3}"
#define MyAppName "Broccoli Desktop"
#define MyAppVersion "0.1.0"
#define MyAppExeName "BroccoliDesktop.exe"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Broccoli
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#SourcePath}\..\dist\installer
OutputBaseFilename=BroccoliDesktop-0.1.0-setup
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible and not arm64
MinVersion=10.0.22000
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#SourcePath}\..\dist\BroccoliDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent; Check: IsWebView2Installed

[Code]
const
  WebView2RuntimeClientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2Runtime(RootKey: Integer; const SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(
    RootKey,
    SubKey + '\' + WebView2RuntimeClientId,
    'pv',
    Version
  ) and (Version <> '');
end;

function IsWebView2Installed(): Boolean;
begin
  Result :=
    HasWebView2Runtime(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients') or
    HasWebView2Runtime(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients') or
    HasWebView2Runtime(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients');
end;

function InitializeSetup(): Boolean;
begin
  Result := IsWebView2Installed();
  if not Result then
    MsgBox(
      'Broccoli Desktop requires the Microsoft Edge WebView2 Runtime. Install it before continuing.',
      mbError,
      MB_OK
    );
end;
