; 论坛转 Halo 安装包脚本（Inno Setup 6）
; 编译：ISCC.exe installer.iss

#define AppName "论坛转Halo"
#define AppNameEn "Forum2Halo"
#define AppVersion "1.0.0"
#define AppPublisher "sk20032004"
#define AppURL "https://github.com/sk20032004/Forum2Halo"
#define ExeName "Forum2Halo.exe"

[Setup]
AppId={{8E4C2A61-5F3B-4A9D-9B77-2C1E6D4A7F30}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=release
OutputBaseFilename={#AppNameEn}-{#AppVersion}-setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#ExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "创建快速启动栏图标"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理工作目录（下载的图片、存档）
Type: filesandordirs; Name: "{app}\work"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 询问是否一并删除配置（含 Halo 令牌）
    if MsgBox('是否同时删除配置文件（其中包含你的 Halo 访问令牌）？',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DeleteFile(ExpandConstant('{app}\config.json'));
    end;
  end;
end;
