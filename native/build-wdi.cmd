@echo off
rem Builds wdi-simple.exe (libwdi's command-line WinUSB installer) into btmulticodec\bin.
rem Needs third_party\libwdi and third_party\wdk8 (msiexec /a of Microsoft's wdfcoinstaller.msi).
setlocal
rem Some shells set this, which stops cmd running tools (vswhere, libwdi's embedder) from the current folder
set NoDefaultCurrentDirectoryInExePath=
set ROOT=%~dp0..
set WDI=%ROOT%\third_party\libwdi
set OUT=%ROOT%\btmulticodec\bin
set LOG=%~dp0build-wdi.log

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
rem not via for /f: the ")" in "(x86)" would close its parenthesised command
"%VSWHERE%" -latest -products * -property installationPath > "%TEMP%\btmulticodec-vs.txt"
set /p VS=<"%TEMP%\btmulticodec-vs.txt"
if not defined VS (echo Visual Studio not found & exit /b 1)
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1

if not exist "%ROOT%\third_party\wdk8\Windows Kits\8.0\redist\wdf\x64\winusbcoinstaller2.dll" (
  echo missing WDK 8 redist: extract wdfcoinstaller.msi to third_party\wdk8 & exit /b 1
)
copy /y "%~dp0libwdi_config.h" "%WDI%\msvc\config.h" >nul

rem 1) The two helper tools, via their projects. Not the .sln (it drags in the ARM64 installer),
rem    and not libwdi_static's project (it evaluates project references that have no x64 config).
rem    The embedder is a build-time host tool with only a Win32 configuration.
type nul > "%LOG%"
for %%p in (libwdi\.msvc\detect_64build:x64 libwdi\.msvc\installer_x64:x64 libwdi\.msvc\embedder:Win32) do (
  for /f "tokens=1,2 delims=:" %%a in ("%%p") do (
    msbuild "%WDI%\%%a.vcxproj" /nologo /v:minimal /p:BuildProjectReferences=false ^
      /p:SolutionDir="%WDI%\\" /p:Configuration=Release /p:Platform=%%b /p:PlatformToolset=v145 ^
      /p:WindowsTargetPlatformVersion=10.0 >> "%LOG%" 2>&1 || (type "%LOG%" & echo BUILD FAILED at %%a & exit /b 1)
  )
)

rem 2) Embed the WinUSB co-installers, the installer helper and the INF/CAT templates
pushd "%WDI%\libwdi"
embedder.exe embedded.h >> "%LOG%" 2>&1 || (popd & type "%LOG%" & echo BUILD FAILED at embedder & exit /b 1)
popd

rem 3) libwdi + wdi-simple + getopt straight into one exe
set OBJ=%TEMP%\btmulticodec-wdi-obj
if not exist "%OBJ%" mkdir "%OBJ%"
cl /nologo /O2 /MT /W3 /D _CRT_SECURE_NO_WARNINGS /D _CRT_NONSTDC_NO_DEPRECATE /D HAVE_STRING_H=1 ^
  /I "%WDI%\msvc" /I "%WDI%\libwdi" /I "%WDI%\examples\getopt" ^
  "%WDI%\libwdi\libwdi.c" "%WDI%\libwdi\libwdi_dlg.c" "%WDI%\libwdi\logging.c" "%WDI%\libwdi\pki.c" ^
  "%WDI%\libwdi\tokenizer.c" "%WDI%\libwdi\vid_data.c" ^
  "%WDI%\examples\wdi-simple.c" "%WDI%\examples\getopt\getopt.c" "%WDI%\examples\getopt\getopt1.c" ^
  /Fo"%OBJ%\\" /Fe"%OUT%\wdi-simple.exe" ^
  /link setupapi.lib newdev.lib ntdll.lib ole32.lib shell32.lib advapi32.lib user32.lib gdi32.lib ^
  comctl32.lib crypt32.lib /MANIFEST:EMBED /MANIFESTUAC:"level='requireAdministrator'" >> "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED at wdi-simple & exit /b 1)
echo built %OUT%\wdi-simple.exe
