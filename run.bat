@echo off
REM Double-click launcher. First run builds .venv and installs deps; later runs
REM just start the app. Runs entirely on this machine -- see README.
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo First run: creating the virtual environment...
  python -m venv .venv || goto :failed
)
call ".venv\Scripts\activate.bat" || goto :failed
python -m streamlit --version >nul 2>&1 || pip install -r requirements.txt || goto :failed
streamlit run streamlit_app.py
goto :eof

:failed
echo.
echo Could not start. Check that Python 3.11+ is installed and on PATH
echo ("python --version" should work in a terminal).
pause
