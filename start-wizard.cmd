@echo off
setlocal
cd /d "%~dp0"
echo diff-analysis - interactive Git revision comparison
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m diff_analysis wizard
) else (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python launcher not found. Please install Python and create .venv first.
    goto :done
  )
  py -3 -m diff_analysis wizard
)
:done
echo.
pause
endlocal
