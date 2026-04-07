@echo off
:: WhisperType — Start as background daemon (no console window)
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0whispertype.pyw"
) else (
    start "" "C:\Users\Foltin Csaba\AppData\Local\Programs\Python\Python312\pythonw.exe" "%~dp0whispertype.pyw"
)
