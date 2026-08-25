@echo off
REM Wrapper so the tracker starts from cmd.exe, Explorer double-click, or a
REM shortcut -- and so run.ps1 works without changing the machine's
REM execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %errorlevel%
