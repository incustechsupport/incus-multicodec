; Inno Setup script (https://jrsoftware.org/isinfo.php, free). Build the PyInstaller bundle first, then:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\multicodec.iss
; Per-user install (no admin); admin rights are only asked for when a dongle is switched to WinUSB.

#define AppVersion "0.1.2"

[Setup]
AppId={{6F6E7C1A-3C7B-4C7E-9B8B-1D2A0C5E7A10}
AppName=Windows Bluetooth Multicodec
AppVersion={#AppVersion}
AppPublisher=Incus Technologies
DefaultDirName={localappdata}\Programs\Windows Bluetooth Multicodec
DefaultGroupName=Windows Bluetooth Multicodec
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=windows-bluetooth-multicodec-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\THIRD_PARTY_NOTICES.md
UninstallDisplayIcon={app}\bt-multicodec.exe

[Files]
Source: "..\dist\windows-bluetooth-multicodec\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Windows Bluetooth Multicodec"; Filename: "{app}\bt-multicodec.exe"
Name: "{group}\Uninstall Windows Bluetooth Multicodec"; Filename: "{uninstallexe}"

[Tasks]
Name: "autostart"; Description: "Start Windows Bluetooth Multicodec when I sign in"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "windows-bluetooth-multicodec"; \
  ValueData: """{app}\bt-multicodec.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\bt-multicodec.exe"; Description: "Start Windows Bluetooth Multicodec now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Hand any dongle Bluetooth Multicodec owns back to Windows (prompts for admin if one exists)
Filename: "{app}\bt-multicodec-cli.exe"; Parameters: "release-adapter"; Flags: runhidden waituntilterminated; RunOnceId: "ReleaseAdapter"
