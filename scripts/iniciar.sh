#!/bin/bash
# Prepara el entorno la primera vez, instala lo que falte y abre Confluence Curator en el navegador.
# Lo usan iniciar-mac.command e iniciar-linux.sh.

cd "$(dirname "$0")/.." || exit 1

pause() {
	echo
	read -r -p "Pulsa Enter para cerrar esta ventana." _
}

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
	if command -v "$candidate" >/dev/null 2>&1 &&
		"$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
		PYTHON="$candidate"
		break
	fi
done

if [ -z "$PYTHON" ]; then
	echo "No se encontró Python 3.11 o superior."
	echo "Instálalo desde https://www.python.org/downloads/ y vuelve a abrir este archivo."
	if command -v open >/dev/null 2>&1; then open "https://www.python.org/downloads/"; fi
	pause
	exit 1
fi

if [ ! -x .venv/bin/python ]; then
	echo "Preparando la instalación por primera vez. Puede tardar unos minutos..."
	if ! "$PYTHON" -m venv .venv; then
		echo "No se pudo crear el entorno de Python."
		echo "En Ubuntu o Debian instala el paquete python3-venv (sudo apt install python3-venv) y vuelve a intentarlo."
		rm -rf .venv
		pause
		exit 1
	fi
fi

if ! .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
	echo "Algo falló al instalar lo necesario. Revisa la conexión a internet y vuelve a intentarlo."
	pause
	exit 1
fi

echo
echo "Deja esta ventana abierta mientras uses la herramienta."
.venv/bin/python -m curator --open
