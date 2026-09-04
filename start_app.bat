@echo off
REM Double-click this file to install dependencies (if needed) and launch
REM ShiftPilot. Safe to run repeatedly -- pip skips packages already
REM installed, so repeat runs are fast.

cd /d "%~dp0"

echo Installing/checking dependencies...
python -m pip install -r requirements.txt --quiet

echo.
echo Starting ShiftPilot on http://localhost:8501 ...
echo (Close this window, or press Ctrl+C, to stop the app.)
echo.

python -m streamlit run app.py

pause
