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

REM Con LINEAS_DETALLE, por defecto NO se envia texto plano duplicado al LLM (evita lineas fantasmas)
REM set UTILESIA_SUPPRESS_PLAIN_WITH_LAYOUT=0

REM Reconstruccion por coordenadas en facturas taller (por defecto ACTIVO). Para desactivar:
REM set UTILESIA_DISABLE_WORD_LAYOUT=1

REM Log del texto enviado al LLM: por defecto DESACTIVADO. Opciones:
REM   - Activar aquí: set UTILESIA_LOG_LLM_USER_PROMPT=1
REM   - O crear utilesia.env (copiar utilesia.env.example)
REM set UTILESIA_LOG_LLM_PROMPT_MAX_CHARS=0

REM Recalcular directUnitCost/lineDiscountPct desde listUnitPrice, netUnitPrice, documentDiscountPct (por defecto activo)
REM set UTILESIA_DERIVE_BC_FROM_EXTENDED=0

REM Tope tokens de respuesta del LLM (evita generaciones interminables). Subir si trunca JSON en presupuestos enormes
REM set UTILESIA_LLM_MAX_TOKENS=8192

echo UtilesIA en http://192.168.10.238:8787  ^(local: http://127.0.0.1:8787^)
echo Ctrl+C para detener.
echo.

uvicorn app:app --host 192.168.10.238 --port 8787

pause
