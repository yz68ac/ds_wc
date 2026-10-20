@echo off
setlocal
set "VSCODE=%USERPROFILE%\AppData\Local\Programs\Microsoft VS Code\Code.exe"
if exist "%VSCODE%" (
  start "" "%VSCODE%" "%~dp0wechat-group-bot.code-workspace"
) else (
  echo VS Code was not found. Open wechat-group-bot.code-workspace manually.
  pause
)
endlocal
