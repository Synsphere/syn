@echo off
setlocal
set "SYN_ROOT=%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SYN_ROOT%Start-Syn.ps1" %*
set "SYN_STATUS=%ERRORLEVEL%"
if not "%SYN_STATUS%"=="0" (
    echo.
    echo Syn exited with status %SYN_STATUS%. Review the diagnostic above.
    pause
)
exit /b %SYN_STATUS%
