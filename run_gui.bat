@echo off
REM Double-clickable GUI launcher for Windows.
REM Runs inside the mp-ternaries conda environment without manual activation.
REM Set MP_API_KEY as a user environment variable (System Properties >
REM Environment Variables) so it is inherited here.

cd /d "%~dp0"

where conda >nul 2>nul
if errorlevel 1 (
  echo conda not found on PATH.
  echo Install conda ^(or use "Anaconda Prompt"^) and run:
  echo   conda env create -f environment.yml
  pause
  exit /b 1
)

conda env list | findstr /b /c:"mp-ternaries " >nul
if errorlevel 1 (
  echo Environment 'mp-ternaries' not found. Creating it now ^(one time^)...
  conda env create -f environment.yml
  if errorlevel 1 ( pause & exit /b 1 )
)

conda run --no-capture-output -n mp-ternaries python gui.py
if errorlevel 1 pause
