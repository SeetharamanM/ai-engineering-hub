"""Command line entry: ``python -m hub_ui`` (run from the ``hub/`` folder)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI to browse, configure and launch the AI Engineering Hub projects")
    parser.add_argument("--host", default=os.environ.get("HUB_HOST", "127.0.0.1"),
                        help="Interface to bind (default 127.0.0.1). Apps launched from the hub bind to the same host.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HUB_PORT", "8600")))
    parser.add_argument("--repo", default=os.environ.get("HUB_REPO_ROOT"),
                        help="Repository root to scan (default: parent of hub/)")
    parser.add_argument("--token", default=os.environ.get("HUB_TOKEN"),
                        help="Require this access token. Auto-generated when binding to a non-loopback host.")
    args = parser.parse_args()

    os.environ["HUB_HOST"] = args.host
    if args.repo:
        os.environ["HUB_REPO_ROOT"] = str(Path(args.repo).resolve())
    if args.token:
        os.environ["HUB_TOKEN"] = args.token

    import uvicorn

    from .server import create_app, make_settings_from_env

    settings = make_settings_from_env()
    app = create_app(settings)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    print(f"\n  AI Engineering Hub UI → http://{shown}:{args.port}")
    print(f"  Scanning projects in  {settings.repo_root}")
    if settings.token:
        print(f"  Access token:         {settings.token}")
    print()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
