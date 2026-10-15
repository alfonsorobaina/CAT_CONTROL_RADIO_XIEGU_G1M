@echo off
cd /d "%~dp0"
python g1m_control.py
if errorlevel 1 pause
