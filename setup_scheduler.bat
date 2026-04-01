@echo off
chcp 65001 >nul 2>&1
echo ============================================================
echo  YouTube Research Pipeline - Windows Task Scheduler Setup
echo ============================================================
echo.

set PYTHON_PATH=python
set SCRIPT_DIR=%~dp0
set SCRIPT_PATH=%SCRIPT_DIR%app\daily_runner.py

echo Project: %SCRIPT_DIR%
echo Script:  %SCRIPT_PATH%
echo Schedule: Daily at 09:00 AM
echo.

:: Create the scheduled task
:: /SC DAILY = run daily
:: /ST 09:00 = at 9:00 AM
:: /TN = task name
:: /TR = command to run
:: /F = force overwrite if exists

schtasks /Create ^
  /SC DAILY ^
  /ST 09:00 ^
  /TN "YouTubeResearchPipeline" ^
  /TR "cmd /c \"cd /d %SCRIPT_DIR% && %PYTHON_PATH% -u app/daily_runner.py >> logs\daily_runner.log 2>&1\"" ^
  /F

if %errorlevel%==0 (
    echo.
    echo [OK] Task "YouTubeResearchPipeline" created successfully!
    echo     Schedule: Daily at 09:00 AM
    echo     Log: %SCRIPT_DIR%logs\daily_runner.log
    echo.
    echo To verify: schtasks /Query /TN "YouTubeResearchPipeline"
    echo To delete: schtasks /Delete /TN "YouTubeResearchPipeline" /F
    echo To run now: schtasks /Run /TN "YouTubeResearchPipeline"
) else (
    echo.
    echo [ERROR] Failed to create task. Try running as Administrator.
)

echo.
pause
