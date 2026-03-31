@chcp 65001 > nul
@echo off
cd /d "%~dp0"
uv run run.py --browser %*
pause
