"""CLI Entrypoint for AOS V2.

Usage:
    python -m aos.v2 --home ABSOLUTE_NEW_HOME serve --bind 127.0.0.1 --port 0 --registry ABSOLUTE_SERVER_REGISTRY
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from aos.v2.api import AOSApiServer


def main(argv: list[str] | None = None, *, server_factory: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aos.v2",
        description="AOS V2 Execution Seam Server",
    )
    parser.add_argument(
        "--home",
        required=True,
        type=Path,
        help="Absolute path to isolated AOS V2 runtime home directory",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    serve_parser = subparsers.add_parser("serve", help="Start the authenticated loopback API server")
    serve_parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Loopback IP to bind (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind (default: 0 for OS-assigned ephemeral port)",
    )
    serve_parser.add_argument(
        "--registry",
        required=True,
        type=Path,
        help="Absolute path to server-owned registry JSON file",
    )

    args = parser.parse_args(argv)

    if not args.home.is_absolute():
        sys.stderr.write("Error: --home path must be a raw absolute path.\n")
        return 1

    if not args.registry.is_absolute():
        sys.stderr.write("Error: --registry path must be a raw absolute path.\n")
        return 1

    from aos.v2.lifecycle import _no_links
    try:
        home_path = _no_links(args.home)
        registry_path = _no_links(args.registry)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    if not registry_path.is_file():
        sys.stderr.write(f"Error: --registry path must be an existing absolute file: {registry_path}\n")
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    factory = server_factory or AOSApiServer
    try:
        server = factory(
            home=home_path,
            registry_path=registry_path,
            bind_host=args.bind,
            port=args.port,
        )
    except Exception as e:
        sys.stderr.write(f"Failed to start AOS V2 API server: {e}\n")
        return 1

    shutdown_event = threading.Event()

    def handle_sig(sig: int, frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    finally:
        server.shutdown_and_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
