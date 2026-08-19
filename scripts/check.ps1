$ErrorActionPreference = "Stop"

& .\.venv\Scripts\ruff.exe format --check .
& .\.venv\Scripts\ruff.exe check .
& .\.venv\Scripts\python.exe -m pytest
