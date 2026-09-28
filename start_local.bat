@echo off
REM Starts the background worker, the web UI and the extension API (three windows).
REM Dashboard: http://localhost:8501   Extension API: http://127.0.0.1:8000
cd /d "%~dp0"
start "GDS worker" cmd /k ".venv\Scripts\python -m app.worker"
start "GDS web UI" cmd /k ".venv\Scripts\streamlit run app/ui.py"
start "GDS extension API" cmd /k ".venv\Scripts\python -m app.api"
