@echo off
chcp 65001 >nul 2>&1
echo ============================================================
echo  YouTube AI Category Research - Windows Task Scheduler Setup
echo ============================================================
echo.

set SCRIPT_DIR=%~dp0
set PAGE_ID=33609d25c5a180bcab08f8662b65f073

echo Project:  %SCRIPT_DIR%
echo Script:   app\category_research.py
echo Notion:   %PAGE_ID%
echo Schedule: Daily at 09:00 AM
echo.

schtasks /Create ^
  /SC DAILY ^
  /ST 09:00 ^
  /TN "YouTubeCategoryResearch" ^
  /TR "cmd /c \"cd /d %SCRIPT_DIR% && set PATH=C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin;%%PATH%% && python -u -m app.category_research --page-id %PAGE_ID% --count 4 --no-whisper >> logs\category_research.log 2>&1\"" ^
  /F

if %errorlevel%==0 (
    echo.
    echo [OK] Task "YouTubeCategoryResearch" created!
    echo     Schedule: Daily at 09:00 AM
    echo     Log: %SCRIPT_DIR%logs\category_research.log
    echo.
    echo To verify:  schtasks /Query /TN "YouTubeCategoryResearch"
    echo To delete:  schtasks /Delete /TN "YouTubeCategoryResearch" /F
    echo To run now: schtasks /Run /TN "YouTubeCategoryResearch"
) else (
    echo.
    echo [ERROR] Failed. Try running as Administrator.
)

echo.
pause
