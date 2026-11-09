from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Freenove rover web console")
    parser.add_argument("--config", type=Path, help="Local calibration JSON")
    parser.add_argument("--demo", action="store_true", help="Synthetic video and simulated actuators; no GPIO access")
    parser.add_argument("--host", help="Listen address; demo defaults to 127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check-config", action="store_true", help="Validate settings without opening camera or GPIO")
    parser.add_argument("--print-default-config", action="store_true")
    parser.add_argument("--token-file", type=Path, help="File containing the web access key")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import default_config, load_config, validate_config
    if args.print_default_config:
        print(json.dumps(default_config(), indent=2))
        return
    try:
        config = load_config(args.config)
        if args.host:
            config["server"]["host"] = args.host
        elif args.demo:
            config["server"]["host"] = "127.0.0.1"
        if args.port is not None:
            config["server"]["port"] = args.port
        validate_config(config)
        if args.check_config:
            print("Configuration valid. No camera or GPIO was opened.")
            return
        token_file = args.token_file or os.environ.get("ROVER_TOKEN_FILE")
        token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else os.environ.get("ROVER_TOKEN", "")
        if not args.demo and len(token) < 20:
            parser.error("Real rover mode requires an access key of at least 20 characters via --token-file or ROVER_TOKEN_FILE")
        from aiohttp import web
        from .server import create_app
        app = create_app(config, demo=args.demo, token=token)
        web.run_app(app, host=config["server"]["host"], port=config["server"]["port"],
                    access_log=None, shutdown_timeout=5)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Configuration error: {exc}\n")


if __name__ == "__main__":
    main()
