@echo off
REM Local MOCK booking website for testing lookups end to end (not a real airline site).
REM Pick "mock_demo" as the website when creating a job. http://127.0.0.1:8765
cd /d "%~dp0"
start "Mock booking site" cmd /k ".venv\Scripts\python -m tests.mock_site.server"
