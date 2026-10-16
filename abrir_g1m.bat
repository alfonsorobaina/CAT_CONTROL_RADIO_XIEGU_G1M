@echo off
rem Abre el panel sin dejar una ventana de comandos abierta.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw g1m_control.py) || (python g1m_control.py || pause)
