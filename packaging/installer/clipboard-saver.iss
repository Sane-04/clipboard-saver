; 剪贴收藏安装包：仅当前用户安装，无需管理员权限。
; 由 packaging/build_portable.py --installer 调用，传入 AppVersion、SourceDir 与 OutputDir。
#ifndef AppVersion
  #error 请通过 /DAppVersion=x.y.z 指定版本
#endif
#ifndef SourceDir
  #error 请通过 /DSourceDir=<PyInstaller 便携目录> 指定程序文件
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#define AppName "剪贴收藏"
#define AppExe "剪贴收藏.exe"

[Setup]
AppId={{774DF7C5-ABBB-4F3D-A910-FB047A6086EC}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Sane-04
AppPublisherURL=https://github.com/Sane-04/clipboard-saver
AppSupportURL=https://github.com/Sane-04/clipboard-saver/issues
; lowest 权限下 {autopf} 指向 %LOCALAPPDATA%\Programs，程序旁的 data/ 可写，无需改动数据位置逻辑。
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=ClipboardSaver-{#AppVersion}-Setup-x64
SetupIconFile=..\..\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
; 程序运行时持有同名互斥量，升级和卸载前提示用户先从托盘退出。
AppMutex=ClipboardSaver.Running

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.DeleteData=是否同时删除剪贴板历史、设置和 API 密钥？%n%n位置：%1%n%n选「否」会保留这些数据，重新安装后可以继续使用。%n如果在程序里把数据改存到了其他位置，那些位置需要手动删除。
english.DeleteData=Also delete clipboard history, settings and the API key?%n%nLocation: %1%n%nChoose "No" to keep them for a future reinstall.%nData moved to custom locations must be deleted manually.
chinesesimplified.DirNotWritable=无法写入所选位置：%n%1%n%n剪贴收藏会把历史数据保存在安装目录里，需要当前用户可写的位置。%n请换一个位置，例如默认位置，或 D:\Apps\剪贴收藏。
english.DirNotWritable=Cannot write to the selected folder:%n%1%n%nClipboard Saver keeps its history inside the install folder, so it must be writable by the current user.%nPlease choose another folder, such as the default one or D:\Apps\ClipboardSaver.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 升级时先清空旧运行库，避免上一版残留的 DLL 与新版混用；data/ 不在此列。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValue = 'ClipboardSaverLocal';

// 免管理员安装且数据写在安装目录旁，所选位置必须对当前用户可写（如 Program Files 不行）。
function DirectoryWritable(Dir: String): Boolean;
var
  Probe: String;
begin
  // 目标目录多半还不存在，向上找到最近的已存在目录做写入测试。
  while not DirExists(Dir) and (ExtractFileDir(Dir) <> Dir) do
    Dir := ExtractFileDir(Dir);
  Probe := AddBackslash(Dir) + 'clipboard-saver-write-test.tmp';
  Result := SaveStringToFile(Probe, '', False);
  if Result then
    DeleteFile(Probe);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and not DirectoryWritable(WizardDirValue) then
  begin
    MsgBox(FmtMessage(CustomMessage('DirNotWritable'), [WizardDirValue]), mbError, MB_OK);
    Result := False;
  end;
end;

// 只移除指向本安装目录的登录启动项；便携版或其他目录开启的启动项保持不变。
procedure RemoveOwnAutostart;
var
  Command: String;
begin
  if RegQueryStringValue(HKCU, RunKey, RunValue, Command) then
    if Pos(Lowercase(ExpandConstant('{app}\{#AppExe}')), Lowercase(Command)) > 0 then
      RegDeleteValue(HKCU, RunKey, RunValue);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, LocationFile: String;
begin
  if CurUninstallStep = usUninstall then
    RemoveOwnAutostart;
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');
    LocationFile := ExpandConstant('{app}\data-locations.json');
    // 静默卸载不弹窗，默认保留数据；交互卸载默认按钮为「否」。
    if (DirExists(DataDir) or FileExists(LocationFile)) and not UninstallSilent then
      if MsgBox(FmtMessage(CustomMessage('DeleteData'), [DataDir]), mbConfirmation,
                MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
        DeleteFile(LocationFile);
      end;
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
