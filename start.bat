@echo off
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0whispertype.pyw"

rem 1) Virtualenv created by install.bat wins.
if exist "%~dp0.venv\Scripts\pythonw.exe" goto venv

rem 2) Explicit override, for machines with an unusual Python layout.
if not defined WHISPERTYPE_PYTHONW goto launcher
if not exist "%WHISPERTYPE_PYTHONW%" goto launcher
start "" "%WHISPERTYPE_PYTHONW%" "%SCRIPT%"
goto :eof

:venv
start "" "%~dp0.venv\Scripts\pythonw.exe" "%SCRIPT%"
goto :eof

:launcher
rem 3) The py launcher's default Python 3, but only if it has the dependencies.
rem    A bare "pythonw" on PATH is very often a different, empty install.
py -3 -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('whisper') else 1)" >nul 2>&1
if errorlevel 1 goto onpath
start "" pyw -3 "%SCRIPT%"
goto :eof

:onpath
rem 4) Last resort: whatever pythonw is on PATH.
where pythonw >nul 2>&1
if errorlevel 1 goto missing
echo [WARN] No interpreter with WhisperType's dependencies found.
echo [WARN] Trying pythonw from PATH - if nothing appears, check voice_daemon.log.
start "" pythonw "%SCRIPT%"
goto :eof

:missing
echo [ERROR] No Python with WhisperType's dependencies was found.
echo Run install.bat, or set WHISPERTYPE_PYTHONW to the pythonw.exe you want.
pause
exit /b 1
