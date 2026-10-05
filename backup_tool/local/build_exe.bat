@echo off
setlocal
cd /d "%~dp0"

python -m pip install pyinstaller
if errorlevel 1 goto :error

python -m PyInstaller --noconfirm --clean --onedir --windowed --name Archie backup_tool\local\gui.py
if errorlevel 1 goto :error

echo.
echo EXE готов:
echo dist\Archie\Archie.exe
echo.
echo ВАЖНО: один раз на компьютере также должен быть установлен Chromium для Playwright:
echo python -m playwright install chromium
pause
exit /b 0

:error
echo.
echo ОШИБКА сборки.
pause
exit /b 1
