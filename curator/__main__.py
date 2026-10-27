import argparse

import uvicorn

from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m curator", description="Inicia la interfaz web de Confluence Curator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
