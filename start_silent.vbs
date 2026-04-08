Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
If CreateObject("Scripting.FileSystemObject").FileExists(".venv\Scripts\pythonw.exe") Then
    WshShell.Run """.venv\Scripts\pythonw.exe"" ""whispertype.pyw""", 0, False
Else
    WshShell.Run """C:\Users\Foltin Csaba\AppData\Local\Programs\Python\Python312\pythonw.exe"" ""whispertype.pyw""", 0, False
End If
