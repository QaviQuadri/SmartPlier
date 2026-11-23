@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B jobhunt.py launch
) else (
  python -B jobhunt.py launch
)
if errorlevel 1 pause
