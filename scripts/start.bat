@echo off
REM Faz 8 — Watchdog launcher (no extra terminal windows).
REM Bu betiği bir kez çift tıklayın; orchestrator + webui + watchdog
REM gizli arka planda koşar. PC açık olduğu sürece çalışır.

cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONPATH=%CD%\src

REM pythonw.exe = console-less Python; arka planda gizli koşar.
start "" "%CD%\.venv\Scripts\pythonw.exe" scripts\watchdog.py

echo Started. Logs: logs\watchdog.log
echo Dashboard: http://127.0.0.1:8501
echo (Bu pencereyi kapatabilirsin — watchdog arka planda kosar.)
timeout /t 5
