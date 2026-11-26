"""Start the server: `python3 -m factory_ui [--port N]`. Ctrl-C stops it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, server


def main(argv: list[str] | None = None, root: Path | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="factory_ui", description="Read-only web UI for the SWE factory."
    )
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    args = parser.parse_args(argv)
    why = config.port_refusal(args.port)
    if why:
        print(f"factory-ui: port {args.port} refused: {why}", file=sys.stderr)
        sys.exit(2)
    try:
        srv = server.make_server(root or config.FACTORY_ROOT, args.port)
    except OSError as e:
        print(
            f"factory-ui: cannot listen on {config.HOST}:{args.port}: {e.strerror}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"factory-ui: listening on {config.HOST}:{args.port}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
