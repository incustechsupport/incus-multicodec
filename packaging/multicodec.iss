; Inno Setup script (https://jrsoftware.org/isinfo.php, free). Build the PyInstaller bundle first, then:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\multicodec.iss
; Per-user install (no admin); admin rights are only asked for when a dongle is switched to WinUSB.

#define AppVersion "0.1.6"

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
Name: "autostart"; Description: "Start Incus Multicodec when I sign in"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "windows-bluetooth-multicodec"; \
  ValueData: """{app}\bt-multicodec.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\bt-multicodec.exe"; Description: "Start Incus Multicodec now"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer silently with /update=1: start the app again afterwards
Filename: "{app}\bt-multicodec.exe"; Flags: nowait; Check: IsUpdate

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

[UninstallRun]
; Hand any dongle Incus Multicodec owns back to Windows (prompts for admin if one exists)
Filename: "{app}\bt-multicodec-cli.exe"; Parameters: "release-adapter"; Flags: runhidden waituntilterminated; RunOnceId: "ReleaseAdapter"
; The optional "run as administrator" task, under its current and pre-0.1.6 names (may need admin to
; delete; harmless if it fails or is absent)
Filename: "schtasks.exe"; Parameters: "/delete /tn ""Incus Multicodec (administrator)"" /f"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAdminTask"
Filename: "schtasks.exe"; Parameters: "/delete /tn ""Windows Bluetooth Multicodec (administrator)"" /f"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveLegacyAdminTask"
