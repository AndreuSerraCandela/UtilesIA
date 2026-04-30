@echo off
REM Siempre arranca desde esta carpeta para que importe el app.py correcto.
cd /d "%~dp0"
echo UtilesIA en: %CD%
echo Parar con Ctrl+C. Abrir http://127.0.0.1:8787/version para ver rutas POST.
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8787 --reload
