@echo off
chcp 65001 >nul 2>&1
echo ============================================================
echo  YouTube Daily Research - Windows Task Scheduler Setup
echo ============================================================
echo.

set SCRIPT_DIR=%~dp0
set SCRIPT_PATH=%SCRIPT_DIR%app\daily_research.py
set PAGE_ID=33509d25c5a180ae87e4dd36fbc5afed

echo Project: %SCRIPT_DIR%
echo Script:  %SCRIPT_PATH%
echo Notion:  %PAGE_ID%
echo Schedule: Daily at 09:00 AM
echo.

schtasks /Create ^
  /SC DAILY ^
  /ST 09:00 ^
  /TN "YouTubeDailyResearch" ^
  /TR "cmd /c \"cd /d %SCRIPT_DIR% && set PATH=C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin;%%PATH%% && python -u app/daily_research.py --page-id %PAGE_ID% >> logs\daily_research.log 2>&1\"" ^
  /F

if %errorlevel%==0 (
    echo.
    echo [OK] Task "YouTubeDailyResearch" created!
    echo     Schedule: Daily at 09:00 AM
    echo     Log: %SCRIPT_DIR%logs\daily_research.log
    echo.
    echo To verify:  schtasks /Query /TN "YouTubeDailyResearch"
    echo To delete:  schtasks /Delete /TN "YouTubeDailyResearch" /F
    echo To run now: schtasks /Run /TN "YouTubeDailyResearch"
) else (
    echo.
    echo [ERROR] Failed. Try running as Administrator.
)

echo.
pause
