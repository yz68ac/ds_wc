@echo off
setlocal
set "VSCODE=%USERPROFILE%\AppData\Local\Programs\Microsoft VS Code\Code.exe"
if exist "%VSCODE%" (
  start "" "%VSCODE%" "%~dp0wechat-deepseek.code-workspace"
) else (
  echo VS Code was not found. Open wechat-deepseek.code-workspace manually.
  pause
)
endlocal
