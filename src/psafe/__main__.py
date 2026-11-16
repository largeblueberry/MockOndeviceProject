"""python -m psafe                                          (config/default.yaml 사용)
python -m psafe --mode threaded                          (실시간, 3스레드. Ctrl+C로 종료)
python -m psafe --scenario scenarios/flicker.yaml --check
python -m psafe --scenario "scenarios/*.yaml" --check    (전체 시나리오, 즉시 실행)
python -m psafe --scenario "scenarios/*.yaml" --check --mode threaded   (전체 시나리오, 실시간)
"""

import argparse
import glob
import logging
import sys

from psafe.app import build
from psafe.core.config import ConfigError, load_config
from psafe.runners import SyncRunner, ThreadedRunner
from psafe.scenario_check import run_scenario


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="psafe", description="Parking tower safety pipeline")
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--mode", choices=["sync", "threaded"], help="override runner.mode")
    ap.add_argument("--scenario", help="scenario file or glob; runs with the dummy source")
    ap.add_argument("--check", action="store_true", help="verify the scenario's expect list (exit 1 on failure)")
    ap.add_argument("--max-frames", type=int, default=None, help="sync mode only")
    ap.add_argument("--seconds", type=float, default=None, help="threaded mode only: stop after N s")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true", help="hide per-decision logs")
    args = ap.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(threadName)-9s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.scenario:
            return _run_scenarios(args)
        cfg = load_config(args.config)
        if args.mode:
            cfg.runner.mode = args.mode
            if cfg.runner.clock == "fake" and args.mode == "threaded":
                cfg.runner.clock = "real"
        c = build(cfg)  #app.py 호출. config.yaml에 따라 부품을 생성한다.
    except (ConfigError, FileNotFoundError, ValueError) as e:
        logging.error("config error: %s", e)
        return 2

    try:
        if cfg.runner.mode == "sync":
            SyncRunner(c, max_frames=args.max_frames).run()
            return 0
        result = ThreadedRunner(c, max_seconds=args.seconds).run()
    except (FileNotFoundError, RuntimeError) as e:  # 모델 / 카메라 / 영상 열기 실패
        logging.error("startup failed: %s", e)
        return 2
    return 1 if result.error else 0


def _run_scenarios(args: argparse.Namespace) -> int:
    paths = sorted(glob.glob(args.scenario)) or [args.scenario]
    mode = args.mode or "sync"
    failed = 0
    for p in paths:
        report = run_scenario(p, args.config, mode=mode)
        if args.check:
            print(report.summary(), flush=True)
            failed += not report.passed
    if args.check and len(paths) > 1:
        print(f"\n{len(paths) - failed}/{len(paths)} scenarios passed ({mode})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
