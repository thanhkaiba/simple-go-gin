; Windows installer (Inno Setup 6): iscc /DVersion=1.2.3 packaging\windows\FaceAttendance.iss
; Installs the PyInstaller folder for the current user (no administrator needed). Data, models and
; downloaded updates live in %LOCALAPPDATA%\FaceAttendance and are kept on uninstall, except updates.
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Dist
  #define Dist "..\..\dist\FaceAttendance"
#endif

[Setup]
AppId={{6C2F4E3A-9B1D-4F8E-A7C5-FACEA77E0DA1}
AppName=Face Attendance
AppVersion={#Version}
AppPublisher=Face Attendance
DefaultDirName={localappdata}\Programs\FaceAttendance
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=FaceAttendance-Setup-{#Version}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\FaceAttendance.exe
CloseApplications=no

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Face Attendance"; Filename: "{app}\FaceAttendance.exe"
Name: "{userdesktop}\Face Attendance"; Filename: "{app}\FaceAttendance.exe"

[InstallDelete]
; The script installer's login shortcut; the program now registers itself.
Type: files; Name: "{userstartup}\Camera attendance.lnk"

[Run]
Filename: "{app}\FaceAttendance.exe"; Description: "Start Face Attendance"; Flags: nowait postinstall

[UninstallRun]
Filename: "{app}\FaceAttendance.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated; RunOnceId: "stop"
Filename: "{app}\FaceAttendance.exe"; Parameters: "remove-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "autostart"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\FaceAttendance\versions"

[Code]
// Stop the running program (this version, an update, or the old script-installed one) before copying files.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -Command "' +
       '$h = Join-Path $env:LOCALAPPDATA ''FaceAttendance''; ' +
       'Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and ' +
       '($_.CommandLine.Contains($h) -or $_.Name -eq ''FaceAttendance.exe'') } | ' +
       'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1500);
  Result := '';
end;
