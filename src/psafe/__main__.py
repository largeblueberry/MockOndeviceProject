"""python -m psafe --config config/default.yaml"""

import argparse
import logging
import sys

from psafe.app import build
from psafe.core.config import ConfigError, load_config
from psafe.runners import SyncRunner


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="psafe", description="Parking tower safety pipeline")
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as e:
        logging.error("config error: %s", e)
        return 2

    if cfg.runner.mode != "sync":
        logging.error("runner.mode=%s not implemented yet (M3+)", cfg.runner.mode)
        return 2

    SyncRunner(build(cfg), max_frames=args.max_frames).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
