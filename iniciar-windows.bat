@echo off
rem Doble clic para abrir Confluence Curator en Windows.
rem La primera vez prepara el entorno e instala lo necesario.
setlocal
cd /d "%~dp0"
title Confluence Curator

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
	echo.
	echo No se encontro Python 3.11 o superior.
	echo Instalalo desde https://www.python.org/downloads/
	echo Durante la instalacion marca la casilla "Add python.exe to PATH".
	echo Luego vuelve a abrir este archivo.
	start "" "https://www.python.org/downloads/"
	pause
	exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
	echo Preparando la instalacion por primera vez. Puede tardar unos minutos...
	%PY% -m venv .venv
	if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto failed

echo.
echo Deja esta ventana abierta mientras uses la herramienta.
".venv\Scripts\python.exe" -m curator --open
if errorlevel 1 pause
exit /b 0

:failed
echo.
echo Algo fallo al instalar lo necesario. Revisa la conexion a internet y vuelve a intentarlo.
pause
exit /b 1
