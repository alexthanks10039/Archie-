@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo Archie - установка зависимостей
echo ==========================================
echo.

python -m pip install --upgrade pip
if errorlevel 1 goto :error

python -m pip install -r requirements.txt
if errorlevel 1 goto :error

python -m pip install -r requirements-browser.txt
if errorlevel 1 goto :error

python -m playwright install chromium
if errorlevel 1 goto :error

echo.
echo Готово.
echo Теперь запусти start_gui.bat
pause
exit /b 0

:error
echo.
echo ОШИБКА установки.
pause
exit /b 1
