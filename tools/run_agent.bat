@echo off
rem 5S Vision camera agent - edit the two lines below, then double-click.
rem Get the token from the Cameras page of the admin menu.
set SERVER=https://YOUR-SERVICE.onrender.com
set TOKEN=PASTE-AGENT-TOKEN-HERE

cd /d "%~dp0"
rem Follows the per-camera schedules set on the Cameras page of the admin menu.
rem Add  --listen 08:00-17:00  to also take on-demand capture requests from the web page.
python camera_agent.py --server %SERVER% --token %TOKEN%
pause
