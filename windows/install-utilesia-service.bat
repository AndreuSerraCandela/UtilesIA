@echo off
REM Instala el servicio UtilesIA (NSSM).
REM NSSM: si falta, el .ps1 descarga nssm-2.24 desde nssm.cc (salvo -NoAutoDownload).
REM        Tambien puede copiar nssm.exe del zip a esta carpeta windows\
REM Ejecutar con clic derecho ^> "Ejecutar como administrador".
REM Parametros opcionales para el .ps1:
REM   install-utilesia-service.bat -HostBinding 192.168.10.238 -Port 8787
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if %errorLevel% equ 0 goto :run

echo.
echo [UtilesIA] Se necesitan permisos de administrador.
echo.
echo No use doble clic normal: abra el menu contextual ^(clic derecho^) sobre este archivo
echo y elija "Ejecutar como administrador".
echo.
echo ^(La elevacion automatica se omitio para evitar bucles de ventanas.^)
echo.
pause
exit /b 1

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-utilesia-service.ps1" %*
set ERR=%errorLevel%
if %ERR% neq 0 echo Error %ERR%
pause
exit /b %ERR%
