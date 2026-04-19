@echo off
rem oto-bot control room — launch Streamlit dashboard
cd /d "%~dp0.."
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m streamlit run dashboard\app.py --server.headless false --server.port 8501 --browser.gatherUsageStats false
pause
