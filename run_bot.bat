@echo off
rem Starts the paper bot and restarts it automatically if it ever stops.
rem To stop for good: press Ctrl+C, then answer Y to "Terminate batch job?"
rem (or just close this window).
cd /d "%~dp0"
title Memecoin paper bot
:loop
python -u paper_bot.py
echo.
echo [%date% %time%] Bot stopped. Restarting in 15 seconds - close this window to stop for good.
timeout /t 15 /nobreak >nul
goto loop
