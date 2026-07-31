"""Entry point: python -m simple_pool <config_path>"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config, parse_config
from .server import PoolServer
from .state import PoolState


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="simple-pool",
        description="Sticky failover router for OpenAI-compatible backends",
    )
    parser.add_argument("config", help="Path to YAML or JSON config file")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("simple_pool")

    raw = load_config(args.config)
    config = parse_config(raw)

    state = PoolState(config["state_path"])
    server = PoolServer(config, state)

    host = config["host"]
    port = config["port"]
    logger.info("listening on %s:%d with %d backends", host, port, len(config["backends"]))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
