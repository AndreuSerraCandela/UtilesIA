@echo off
chcp 65001 >nul
title UtilesIA - Uvicorn 8787
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo No existe el entorno virtual .venv
    echo Ejecute primero: python -m venv .venv ^&^& .venv\Scripts\activate.bat ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

REM Opcional OCR: descomente y ajuste la ruta si Tesseract no esta en PATH
REM set TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe

REM Tablas PyMuPDF (opcional): por defecto DESACTIVADO; puede dar celdas mal alineadas
REM set UTILESIA_ENABLE_TABLE_EXTRACTION=1

echo UtilesIA en http://192.168.10.238:8787  ^(local: http://127.0.0.1:8787^)
echo Ctrl+C para detener.
echo.

uvicorn app:app --host 192.168.10.238 --port 8787

pause
