@echo off
rem Builds ldac.dll (Sony libldac encoder + ABR) from third_party\libldac with MSVC.
rem Full compiler output goes to native\build.log.
setlocal
rem Some shells set this, which stops cmd running tools (vswhere, libwdi's embedder) from the current folder
set NoDefaultCurrentDirectoryInExePath=
set ROOT=%~dp0..
set SRC=%ROOT%\third_party\libldac
set OUT=%ROOT%\btmulticodec\bin
set LOG=%~dp0build.log

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
rem not via for /f: the ")" in "(x86)" would close its parenthesised command
"%VSWHERE%" -latest -products * -property installationPath > "%TEMP%\btmulticodec-vs.txt"
set /p VS=<"%TEMP%\btmulticodec-vs.txt"
if not defined VS (echo Visual Studio not found & exit /b 1)
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1

if not exist "%OUT%" mkdir "%OUT%"
set OBJ=%TEMP%\btmulticodec-obj
if not exist "%OBJ%" mkdir "%OBJ%"
rem C4305/C4244: libldac's float tables and fixed-point casts, harmless
cl /nologo /O2 /LD /W3 /MT /wd4305 /wd4244 ^
  /D "LDACBT_API=__declspec(dllexport)" /D "LDAC_ABR_API=__declspec(dllexport)" ^
  /FI "%~dp0compat\msvc_compat.h" /I "%~dp0compat" ^
  /I "%SRC%\inc" /I "%SRC%\abr\inc" /I "%SRC%\src" ^
  "%SRC%\src\ldaclib.c" "%SRC%\src\ldacBT.c" "%SRC%\abr\src\ldacBT_abr.c" ^
  /Fo"%OBJ%\\" /Fe"%OUT%\ldac.dll" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\ldac.exp" "%OUT%\ldac.lib" 2>nul
echo built %OUT%\ldac.dll

rem aptX / aptX HD encoder (libfreeaptx, LGPL-2.1), kept as its own DLL
set APTX=%ROOT%\third_party\libfreeaptx
cl /nologo /O2 /LD /W3 /MT /I "%APTX%" "%APTX%\freeaptx.c" /Fo"%OBJ%\\" /Fe"%OUT%\aptx.dll" ^
  /link /DEF:"%~dp0aptx.def" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\aptx.exp" "%OUT%\aptx.lib" 2>nul
echo built %OUT%\aptx.dll

rem SBC encoder (BlueZ libsbc, LGPL-2.1), for speakers without LDAC/aptX; portable C primitives only
set SBC=%ROOT%\third_party\sbc
cl /nologo /O2 /LD /W3 /MT /wd4244 /wd4267 /FI "%~dp0compat\sbc_compat.h" /I "%SBC%" /I "%SBC%\sbc" ^
  "%SBC%\sbc\sbc.c" "%SBC%\sbc\sbc_primitives.c" /Fo"%OBJ%\\" /Fe"%OUT%\sbc.dll" ^
  /link /DEF:"%~dp0sbc.def" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\sbc.exp" "%OUT%\sbc.lib" 2>nul
echo built %OUT%\sbc.dll

rem AAC: our own thin wrapper around Windows' Media Foundation AAC encoder (no third-party code)
cl /nologo /O2 /LD /W3 /MT /EHsc "%~dp0aacmf.cpp" /Fo"%OBJ%\\" /Fe"%OUT%\aacmf.dll" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\aacmf.exp" "%OUT%\aacmf.lib" 2>nul
echo built %OUT%\aacmf.dll
