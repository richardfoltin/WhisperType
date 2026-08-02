' WhisperType -- launch with no console window at all (used by the Startup shortcut).
' Resolution order: repo virtualenv, WHISPERTYPE_PYTHONW override, the "pyw" launcher's
' default Python 3, then plain "pythonw" from PATH. The launcher is preferred over PATH
' because a bare pythonw.exe on PATH is very often a different, dependency-free install.
Option Explicit

Dim fso, shell, base, script, venvPy, override

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base
script = base & "\whispertype.pyw"
venvPy = base & "\.venv\Scripts\pythonw.exe"

If fso.FileExists(venvPy) Then
    shell.Run """" & venvPy & """ """ & script & """", 0, False
    WScript.Quit
End If

override = shell.ExpandEnvironmentStrings("%WHISPERTYPE_PYTHONW%")
If override <> "%WHISPERTYPE_PYTHONW%" And fso.FileExists(override) Then
    shell.Run """" & override & """ """ & script & """", 0, False
    WScript.Quit
End If

If fso.FileExists(shell.ExpandEnvironmentStrings("%SystemRoot%\pyw.exe")) Then
    shell.Run "pyw -3 """ & script & """", 0, False
Else
    shell.Run "pythonw """ & script & """", 0, False
End If
