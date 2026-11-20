@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Устанавливаю распознавание речи faster-whisper...
python -m pip install faster-whisper
if errorlevel 1 (
  echo Не получилось установить. Проверьте интернет и попробуйте ещё раз.
  pause
  exit /b 1
)
echo Готово. Модель распознавания скачается сама при первой нарезке с распознаванием.
pause
