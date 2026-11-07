@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo First run: installing a small helper called uv. This takes about a minute.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
set OPEN_BROWSER=1
echo Starting Tickover. Keep this window open while you study. Close it to stop the app.
uv run app.py
pause
