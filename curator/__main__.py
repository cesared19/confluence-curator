import argparse
import socket
import sys
import threading
import webbrowser

if sys.version_info < (3, 11):
    sys.exit("Confluence Curator necesita Python 3.11 o superior: https://www.python.org/downloads/")


def running(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m curator", description="Inicia la interfaz web de Confluence Curator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="abre el navegador cuando la herramienta está lista")
    args = parser.parse_args()
    address = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"

    if running("127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host, args.port):
        # Abrir el lanzador dos veces no debe fallar: si ya está en marcha, solo se abre el navegador.
        print(f"Confluence Curator ya está en marcha en {address}")
        if args.open:
            webbrowser.open(address)
        return

    import uvicorn

    from .web import create_app

    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(address,)).start()
    print(f"Confluence Curator en {address} (para detenerla, cierra esta ventana o pulsa Ctrl+C)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
