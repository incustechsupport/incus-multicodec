; Inno Setup script (https://jrsoftware.org/isinfo.php, free). Build the PyInstaller bundle first, then:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\multicodec.iss
; Per-user install (no admin); admin rights are only asked for when a dongle is switched to WinUSB.

; The version comes from btmulticodec\__init__.py (__version__ = "x.y.z"), the one place it's set:
; the app compares it with the update manifest, so the two must never differ
#define VersionLine FileRead(FileOpen(AddBackslash(SourcePath) + "..\btmulticodec\__init__.py"))
#define AppVersion Copy(VersionLine, Pos('"', VersionLine) + 1, RPos('"', VersionLine) - Pos('"', VersionLine) - 1)

[Setup]
AppId={{6F6E7C1A-3C7B-4C7E-9B8B-1D2A0C5E7A10}
AppName=Incus Multicodec
AppVersion={#AppVersion}
AppPublisher=Incus Technologies Limited
AppPublisherURL=https://software.incustech.app/multicodec/
AppSupportURL=https://software.incustech.app/multicodec/report-a-bug/
DefaultDirName={localappdata}\Programs\Incus Multicodec
DefaultGroupName=Incus Multicodec
; Up to 0.1.5 the product was "Windows Bluetooth Multicodec". Updates keep that install folder (nothing
; depends on its name) but move the Start menu entries to the new name.
UsePreviousGroup=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=incus-multicodec-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\TERMS.txt
UninstallDisplayIcon={app}\bt-multicodec.exe

[Files]
Source: "..\dist\incus-multicodec\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; Where people look for them (the bundle's own copies sit in _internal, next to licenses\)
Source: "..\TERMS.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_NOTICES.txt"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{userprograms}\Windows Bluetooth Multicodec"

[Icons]
Name: "{group}\Incus Multicodec"; Filename: "{app}\bt-multicodec.exe"
Name: "{group}\Licence, privacy and third-party notices"; Filename: "{app}\TERMS.txt"
Name: "{group}\Uninstall Incus Multicodec"; Filename: "{uninstallexe}"

[Tasks]
; Offered on a first install only: after that "Start at login" is the app's own setting (tray menu), so
; neither an update nor running the installer again turns it back on. The uninstaller always deletes
; the Run value, however it was set.
Name: "autostart"; Description: "Start Incus Multicodec when I sign in"; Check: not WasInstalled

[Run]
Filename: "{app}\bt-multicodec.exe"; Description: "Start Incus Multicodec now"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer silently with /update=1: start the app again afterwards
Filename: "{app}\bt-multicodec.exe"; Flags: nowait; Check: IsUpdate

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValue = 'windows-bluetooth-multicodec';
  { Held by the running app; signalling the event asks it to quit cleanly (SINGLE_INSTANCE_MUTEX and
    QUIT_EVENT in btmulticodec\tray.py, which also opens them to normal, non-admin processes) }
  AppMutex = 'Local\IncusMulticodec';
  QuitEvent = 'Local\IncusMulticodecQuit';
  EVENT_MODIFY_STATE = $0002;
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F6E7C1A-3C7B-4C7E-9B8B-1D2A0C5E7A10}_is1';

var
  PreviouslyInstalled: Boolean;

function OpenEventW(Access: Cardinal; Inherit: BOOL; Name: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(Handle: THandle): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

function InitializeSetup: Boolean;
begin
  { Checked before this run writes its own uninstall entry }
  PreviouslyInstalled := RegValueExists(HKCU, UninstallKey, 'UninstallString');
  Result := True;
end;

function WasInstalled: Boolean;
begin
  Result := PreviouslyInstalled or IsUpdate;
end;

procedure AskAppToQuit;
var
  Handle: THandle;
begin
  Handle := OpenEventW(EVENT_MODIFY_STATE, False, QuitEvent);
  if Handle <> 0 then
  begin
    SetEvent(Handle);
    CloseHandle(Handle);
  end;
end;

{ Ask the app to quit, wait for it (an in-app update starts us while it's still shutting down), and
  only then end whatever is left, so a file in use can't abort a silent update. A clean quit restores
  the speakers' mute state and lets go of the adapter; a forced one can't. True once it's gone. }
function CloseRunningApp(WaitSeconds: Integer): Boolean;
var
  I, Code: Integer;
begin
  if CheckForMutexes(AppMutex) then
    AskAppToQuit;
  for I := 1 to WaitSeconds * 4 do
  begin
    if not CheckForMutexes(AppMutex) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(250);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM bt-multicodec.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1000);
  Result := not CheckForMutexes(AppMutex);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsUpdate then
    CloseRunningApp(30)
  else if CheckForMutexes(AppMutex) then
    CloseRunningApp(20);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WasInstalled and WizardIsTaskSelected('autostart') then
    RegWriteStringValue(HKCU, RunKey, RunValue, '"' + ExpandConstant('{app}\bt-multicodec.exe') + '"');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { usUninstall comes after "Are you sure?" and before [UninstallRun]: close the app there, since it
    holds the dongle (release-adapter needs it) and its own files }
  if CurUninstallStep = usUninstall then
    CloseRunningApp(20);
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKCU, RunKey, RunValue);
end;

[UninstallRun]
; Hand any dongle Incus Multicodec owns back to Windows (prompts for admin if one exists)
Filename: "{app}\bt-multicodec-cli.exe"; Parameters: "release-adapter"; Flags: runhidden waituntilterminated; RunOnceId: "ReleaseAdapter"
; The optional "run as administrator" task (current and pre-0.1.6 names) and its copy under Program
; Files. Removing them needs admin rights, which this per-user uninstaller doesn't have: the CLI asks
; for them (one UAC prompt), and only when the task or the copy exists
Filename: "{app}\bt-multicodec-cli.exe"; Parameters: "remove-admin-task"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAdminTask"
