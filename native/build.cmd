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

rem aptX and aptX HD: Qualcomm's encoders from AOSP (Apache-2.0), one DLL each because they share
rem internal symbol names. native\aptx_aosp.c gives both the same small C API.
set AOSPX=%ROOT%\third_party\aosp-aptx
if not exist "%OBJ%\aptx" mkdir "%OBJ%\aptx"
cl /nologo /O2 /LD /W3 /MT /wd4244 /I "%AOSPX%\encoder_for_aptx\include" /I "%AOSPX%\encoder_for_aptx\src" ^
  "%~dp0aptx_aosp.c" "%AOSPX%\encoder_for_aptx\src\aptXbtenc.c" "%AOSPX%\encoder_for_aptx\src\ProcessSubband.c" ^
  "%AOSPX%\encoder_for_aptx\src\QmfConv.c" "%AOSPX%\encoder_for_aptx\src\QuantiseDifference.c" ^
  /Fo"%OBJ%\aptx\\" /Fe"%OUT%\aptx.dll" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\aptx.exp" "%OUT%\aptx.lib" 2>nul
echo built %OUT%\aptx.dll
if not exist "%OBJ%\aptxhd" mkdir "%OBJ%\aptxhd"
cl /nologo /O2 /LD /W3 /MT /wd4244 /D APTX_HD /I "%AOSPX%\encoder_for_aptxhd\include" /I "%AOSPX%\encoder_for_aptxhd\src" ^
  "%~dp0aptx_aosp.c" "%AOSPX%\encoder_for_aptxhd\src\aptXHDbtenc.c" "%AOSPX%\encoder_for_aptxhd\src\ProcessSubband.c" ^
  "%AOSPX%\encoder_for_aptxhd\src\QmfConv.c" "%AOSPX%\encoder_for_aptxhd\src\QuantiseDifference.c" ^
  /Fo"%OBJ%\aptxhd\\" /Fe"%OUT%\aptxhd.dll" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\aptxhd.exp" "%OUT%\aptxhd.lib" 2>nul
echo built %OUT%\aptxhd.dll

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

rem Process-loopback capture (apps' audio before the speaker's effects): our own code, Windows APIs only
cl /nologo /O2 /LD /W3 /MT /EHsc /std:c++17 "%~dp0procloop.cpp" /Fo"%OBJ%\\" /Fe"%OUT%\procloop.dll" > "%LOG%" 2>&1
if errorlevel 1 (type "%LOG%" & echo BUILD FAILED, see %LOG% & exit /b 1)
del "%OUT%\procloop.exp" "%OUT%\procloop.lib" 2>nul
echo built %OUT%\procloop.dll
