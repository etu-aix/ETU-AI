"""Serve the local UI and ETU 2 bridge from one FastAPI process."""

import socket

import uvicorn
from api_server import app


def main() -> None:
    listeners = []
    try:
        for port in (8900, 8901):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", port))
            listener.listen(2048)
        config = uvicorn.Config(app, host="127.0.0.1", proxy_headers=False, log_level="info")
        server = uvicorn.Server(config)
        app.state.uvicorn_server = server
        server.run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


if __name__ == "__main__":
    main()
