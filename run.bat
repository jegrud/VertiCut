@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Не найден Python. Установите Python 3.10 или новее: https://www.python.org/downloads/
  echo При установке отметьте галочку "Add Python to PATH".
  pause
  exit /b 1
)

if exist "ffmpeg\bin\ffmpeg.exe" goto ffmpeg_ok
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Не найден FFmpeg. Установите его командой:
  echo     winget install Gyan.FFmpeg
  echo и запустите программу заново.
  pause
  exit /b 1
)
:ffmpeg_ok

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю библиотеку для окна программы - PySide6...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    pause
    exit /b 1
  )
)

if "%1"=="--check" (
  echo Всё готово к запуску.
  exit /b 0
)
start "" pythonw main.py
