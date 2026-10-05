@echo off
setlocal
cd /d "%~dp0"

python gui.py
if errorlevel 1 (
  echo.
  echo Archie завершился с ошибкой.
  pause
)
