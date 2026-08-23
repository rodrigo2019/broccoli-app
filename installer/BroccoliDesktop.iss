#define MyAppId "{{7C87D246-7E61-48A2-A0C4-57B3C56180B3}"
#define MyAppName "Broccoli Desktop"
#define MyAppVersion "0.1.0"
#define MyAppExeName "BroccoliDesktop.exe"
; Must match broccoli_desktop.branding.APPLICATION_IDENTITY: the running
; process claims that identity at startup, and a shortcut pinned under a
; different one becomes a second taskbar button that never lights up.
#define MyAppUserModelId "Broccoli.BroccoliDesktop"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Broccoli
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#SourcePath}\..\dist\installer
OutputBaseFilename=BroccoliDesktop-{#MyAppVersion}-setup
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; setup.exe is the first thing a user sees of the application, and the only
; file they download; without this it carries Inno Setup's own icon. The
; Start Menu and desktop shortcuts need no equivalent -- they inherit the icon
; PyInstaller embedded in the executable they point at.
SetupIconFile={#SourcePath}\..\broccoli_desktop\static\images\broccoli_icon.ico
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
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelId}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelId}"

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
