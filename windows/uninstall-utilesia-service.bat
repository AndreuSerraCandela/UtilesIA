@echo off
REM Desinstala el servicio UtilesIA. Clic derecho ^> "Ejecutar como administrador".
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if %errorLevel% equ 0 goto :run

echo.
echo [UtilesIA] Se necesitan permisos de administrador.
echo.
echo Clic derecho en este archivo ^> "Ejecutar como administrador".
echo.
pause
exit /b 1

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove-utilesia-service.ps1" %*
set ERR=%errorLevel%
if %ERR% neq 0 echo Error %ERR%
pause
exit /b %ERR%
