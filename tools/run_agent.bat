@echo off
rem 5S Vision camera agent - edit the two lines below, then double-click.
rem Get the token from the Cameras page of the admin menu.
set SERVER=https://YOUR-SERVICE.onrender.com
set TOKEN=PASTE-AGENT-TOKEN-HERE

cd /d "%~dp0"
rem Capture every camera at fixed times each day. See camera_agent.py --help for --random and --listen.
python camera_agent.py --server %SERVER% --token %TOKEN% --times 09:30,14:30
pause
