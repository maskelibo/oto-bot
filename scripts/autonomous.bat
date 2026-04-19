@echo off
rem oto-bot autonomous research loop — infinite, target-seeking
cd /d "%~dp0.."
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist logs mkdir logs
".venv\Scripts\python.exe" -m oto_bot.main autonomous --markets "crypto,forex,us_equities" --strategies "day,swing,scalp" --max-cycles 0 --pause 2
pause
