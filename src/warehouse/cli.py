"""Command line entry point for shared service lifecycle tasks."""

from __future__ import annotations

import argparse
from pathlib import Path

from warehouse.config import load_settings, setup_task_logger
from warehouse.services import services_down, services_status, services_up


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warehouse-services",
        description="Start, stop, and inspect the shared Doris and Metabase services.",
    )
    parser.add_argument("--env-file", help="Use an explicit project environment file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("services-up", help="Start Compose services and wait for Doris")
    commands.add_parser("services-down", help="Stop containers while preserving named volumes")
    commands.add_parser("services-status", help="Show Compose and Doris status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env_path = Path(args.env_file).expanduser().resolve() if args.env_file else None
    settings = load_settings(env_file=env_path)
    logger, log_file = setup_task_logger(args.command, settings)
    logger.info("Starting [%s], log file: %s", args.command, log_file)
    actions = {
        "services-up": services_up,
        "services-down": services_down,
        "services-status": services_status,
    }
    return actions[args.command](settings, logger)


if __name__ == "__main__":
    raise SystemExit(main())
