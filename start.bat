@echo off
:: WhisperType — Start as background daemon (no console window)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0whispertype.pyw"
