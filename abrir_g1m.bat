@echo off
cd /d "%~dp0"
python g1m_control.py --puerto COM6 --baudios 19200 --dir 0x70
if errorlevel 1 pause
