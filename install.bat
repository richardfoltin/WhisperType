@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   WhisperType Installer
echo   Push-to-talk voice dictation for Windows
echo ============================================
echo.

:: ── Check Python ────────────────────────────────────────────────────────────

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo Please install Python 3.10+ from https://python.org
    echo IMPORTANT: Check "Add python.exe to PATH" during installation!
    echo.
    echo After installing Python, run this installer again.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)

if %PYMAJOR% LSS 3 (
    echo [ERROR] Python 3.10+ required, found %PYVER%
    pause
    exit /b 1
)
if %PYMAJOR%==3 if %PYMINOR% LSS 10 (
    echo [ERROR] Python 3.10+ required, found %PYVER%
    pause
    exit /b 1
)

echo [OK] Python %PYVER% detected.
echo.

:: ── Create venv ─────────────────────────────────────────────────────────────

set VENV=%~dp0.venv

if not exist "%VENV%\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
)
echo.

:: ── Install dependencies ────────────────────────────────────────────────────

echo Installing dependencies (this may take a while)...
echo.

:: Install PyTorch with CUDA support first
echo Installing PyTorch with CUDA support...
"%VENV%\Scripts\pip.exe" install torch --index-url https://download.pytorch.org/whl/cu124 --quiet
if errorlevel 1 (
    echo [WARN] CUDA PyTorch failed, falling back to CPU version...
    "%VENV%\Scripts\pip.exe" install torch --quiet
)
echo.

:: Install remaining requirements
echo Installing remaining packages...
"%VENV%\Scripts\pip.exe" install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)

echo [OK] All dependencies installed.
echo.

:: ── Pre-download Whisper model ─────────────────────────────────────────────

echo Downloading Whisper model (large-v3-turbo)...
echo This is ~1.5 GB and only needs to happen once.
echo.
"%VENV%\Scripts\python.exe" -c "import whisper; whisper.load_model('large-v3-turbo', device='cpu')"
if errorlevel 1 (
    echo [WARN] Model download failed. It will be downloaded on first launch instead.
) else (
    echo [OK] Whisper model downloaded and cached.
)
echo.

:: ── Copy config ─────────────────────────────────────────────────────────────

set CONFIG_DIR=%USERPROFILE%\.whispertype
set CONFIG_FILE=%CONFIG_DIR%\config.json

if not exist "%CONFIG_DIR%" (
    mkdir "%CONFIG_DIR%"
)

if not exist "%CONFIG_FILE%" (
    copy "%~dp0config.template.json" "%CONFIG_FILE%" >nul
    echo [OK] Config created at %CONFIG_FILE%
    echo     Edit this file to change language, hotkey, model, etc.
) else (
    echo [OK] Config already exists at %CONFIG_FILE%
)
echo.

:: ── Create Startup shortcut ─────────────────────────────────────────────────

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP%\WhisperType.lnk
set PYTHONW=%VENV%\Scripts\pythonw.exe
set SCRIPT=%~dp0whispertype.pyw

echo Creating startup shortcut...
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%PYTHONW%'; $s.Arguments = '\"%SCRIPT%\"'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'WhisperType - Voice Dictation'; $s.Save()"

if exist "%SHORTCUT%" (
    echo [OK] Startup shortcut created. WhisperType will start with Windows.
) else (
    echo [WARN] Could not create startup shortcut. You can run start.bat manually.
)
echo.

:: ── Done ────────────────────────────────────────────────────────────────────

echo ============================================
echo   Installation complete!
echo.
echo   Run start.bat to launch WhisperType.
echo   It will appear as a tray icon.
echo.
echo   Double-tap Right Ctrl to start recording.
echo   Single tap Right Ctrl to stop.
echo ============================================
echo.
pause
