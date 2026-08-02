@echo off
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0whispertype.pyw"
echo Starting WhisperType in debug mode...
echo.

if exist "%~dp0.venv\Scripts\python.exe" goto venv

if not defined WHISPERTYPE_PYTHON goto launcher
if not exist "%WHISPERTYPE_PYTHON%" goto launcher
"%WHISPERTYPE_PYTHON%" "%SCRIPT%"
goto done

:venv
"%~dp0.venv\Scripts\python.exe" "%SCRIPT%"
goto done

:launcher
py -3 -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('whisper') else 1)" >nul 2>&1
if errorlevel 1 goto onpath
py -3 "%SCRIPT%"
goto done

:onpath
where python >nul 2>&1
if errorlevel 1 goto missing
echo [WARN] No interpreter with WhisperType's dependencies found; trying PATH python.
python "%SCRIPT%"
goto done

:missing
echo [ERROR] No Python with WhisperType's dependencies was found.
echo Run install.bat, or set WHISPERTYPE_PYTHON to the python.exe you want.
pause
exit /b 1

:done
echo.
echo WhisperType exited. See voice_daemon.log ^(previous run: voice_daemon.prev.log^).
pause
