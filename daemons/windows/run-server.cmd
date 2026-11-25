@echo off
rem pylon-server on Windows: the daemon in server mode (config.json "mode": "server")
rem starts the server on http://127.0.0.1:8910, its tray icon, and this machine's capabilities.
cd /d "%~dp0\..\.."
set PYTHONIOENCODING=utf-8
if not exist data mkdir data
py -3.10 daemons\windows\agent.py >> data\pylon-daemon.log 2>&1
