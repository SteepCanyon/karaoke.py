@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Installing Python dependencies...
python -m pip install --upgrade pip
python -m pip install pyinstaller python-vlc yt-dlp requests PySocks
if errorlevel 1 goto :fail

echo [2/4] Checking bundled resources...
if not exist "ffmpeg\ffmpeg.exe" (
  echo ERROR: ffmpeg\ffmpeg.exe not found.
  echo Put ffmpeg.exe into the ffmpeg folder next to this script.
  goto :fail
)
if not exist "vlc_bundle\libvlc.dll" echo WARNING: vlc_bundle\libvlc.dll not found - installed VLC will be used.
if not exist "zapret\Zapret.exe" echo WARNING: zapret\Zapret.exe not found - bypass will be disabled.

echo [3/4] Building EXE with PyInstaller...
set EXTRA=
if exist "zapret\Zapret.exe" set EXTRA=%EXTRA% --add-data "zapret;zapret"
if exist "node_server\build\main.js" set EXTRA=%EXTRA% --add-data "node_server;node_server"
if exist "vlc_bundle\libvlc.dll" set EXTRA=%EXTRA% --add-data "vlc_bundle;vlc_bundle"

pyinstaller --noconfirm --windowed --onedir --name KaraokePlayer --icon AppIcon.ico ^
  --add-data "ffmpeg;ffmpeg" ^
  --hidden-import vlc --hidden-import yt_dlp --collect-all yt_dlp %EXTRA% karaoke_win.py
if errorlevel 1 goto :fail

echo [4/4] Done: dist\KaraokePlayer\KaraokePlayer.exe
goto :eof

:fail
echo BUILD FAILED
exit /b 1
