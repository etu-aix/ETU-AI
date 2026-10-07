"""Serve the local UI and ETU 2 bridge from one FastAPI process."""

import socket

import uvicorn


def main() -> None:
    listeners = []
    try:
        for port in (8900, 11346):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            listener.bind(("127.0.0.1", port))
            listener.listen(2048)
        config = uvicorn.Config("api_server:app", host="127.0.0.1", proxy_headers=False, log_level="info")
        uvicorn.Server(config).run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


if __name__ == "__main__":
    main()
