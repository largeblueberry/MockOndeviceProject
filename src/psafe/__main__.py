"""python -m psafe --config config/default.yaml
python -m psafe --scenario scenarios/flicker.yaml --check
python -m psafe --scenario "scenarios/*.yaml" --check      (all scenarios)
"""

import argparse
import glob
import logging
import sys

from psafe.app import build
from psafe.core.config import ConfigError, load_config
from psafe.runners import SyncRunner
from psafe.scenario_check import run_scenario


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="psafe", description="Parking tower safety pipeline")
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--scenario", help="scenario file or glob; runs with the dummy source + fake clock")
    ap.add_argument("--check", action="store_true", help="verify the scenario's expect list (exit 1 on failure)")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true", help="hide per-decision logs")
    args = ap.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.scenario:
            return _run_scenarios(args)
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        logging.error("config error: %s", e)
        return 2

    if cfg.runner.mode != "sync":
        logging.error("runner.mode=%s not implemented yet (M3+)", cfg.runner.mode)
        return 2

    SyncRunner(build(cfg), max_frames=args.max_frames).run()
    return 0


def _run_scenarios(args: argparse.Namespace) -> int:
    paths = sorted(glob.glob(args.scenario)) or [args.scenario]
    failed = 0
    for p in paths:
        report = run_scenario(p, args.config)
        if args.check:
            print(report.summary())
            failed += not report.passed
    if args.check and len(paths) > 1:
        print(f"\n{len(paths) - failed}/{len(paths)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
