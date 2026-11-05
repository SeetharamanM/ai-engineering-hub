@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Creating hub virtual environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python -m hub_ui %*
