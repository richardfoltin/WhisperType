@echo off
:: WhisperType — Start with console visible for debugging
cd /d "%~dp0"
echo Starting WhisperType in debug mode...
echo.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0whispertype.pyw"
) else (
    "C:\Users\Foltin Csaba\AppData\Local\Programs\Python\Python312\python.exe" "%~dp0whispertype.pyw"
)
echo.
echo WhisperType exited.
pause
