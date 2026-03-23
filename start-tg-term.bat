@echo off
REM tg-term auto-start script for Windows
REM Place a shortcut to this file in shell:startup to run at login

cd /d "%~dp0"

REM Restart loop: if tg-term crashes, wait 5s and restart
:loop
echo Starting tg-term...
python tg-term.py
echo tg-term exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
