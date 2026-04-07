@echo off
:: WhisperType — Start with console visible for debugging
echo Starting WhisperType in debug mode...
echo Log output will appear below. Press Ctrl+C to stop.
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0whispertype.pyw"
echo.
echo WhisperType exited.
pause
