"""Administrative/debug CLI for AOS Runtime V1.

Normal orchestration remains inside the detached runtime. The CLI is a client of
that runtime API; it is not the orchestration authority.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from aos.runtime_client import RuntimeClient, RuntimeClientError


def default_runtime_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "AOS" / "runtime-v1"
    return Path.home() / ".aos" / "runtime-v1"


def client_from_args(args: argparse.Namespace) -> RuntimeClient:
    root = Path(args.runtime_root).expanduser().resolve()
    config_path = root / "runtime-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = f"http://127.0.0.1:{int(config.get('port', 8770))}"
    token_path = Path(config["runtime_token_path"])
    return RuntimeClient(base_url, token_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Runtime V1 admin/debug client")
    parser.add_argument("--runtime-root", default=str(default_runtime_root()))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    cont = sub.add_parser("continue", help="Submit a goal to the detached runtime")
    cont.add_argument("--goal", required=True)
    cont.add_argument("--project")
    cont.add_argument("--constraint", action="append", default=[])
    cont.add_argument("--red-line", action="append", default=[])
    cont.add_argument("--batches-per-cycle", type=int, default=1)
    cont.add_argument("--iterations-per-batch", type=int, default=30)

    show = sub.add_parser("show")
    show.add_argument("command_id")

    events = sub.add_parser("events")
    events.add_argument("command_id")
    events.add_argument("--after-seq", type=int, default=0)

    proj = sub.add_parser("project-state", help="Expose current project state from the detached runtime")
    proj.add_argument("--command-id", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = client_from_args(args)
        if args.command == "status":
            value = client.health()
        elif args.command == "continue":
            value = client.continue_project(
                goal=args.goal,
                project_id=args.project,
                constraints=args.constraint,
                red_lines=args.red_line,
                max_batches_per_cycle=args.batches_per_cycle,
                max_iterations_per_batch=args.iterations_per_batch,
                continuous=True,
            )
        elif args.command == "show":
            value = client.command(args.command_id)
        elif args.command == "project-state":
            value = client.current_project_state(args.command_id)
        else:
            value = client.events(args.command_id, after_seq=args.after_seq)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except RuntimeClientError as exc:
        print(f"AOS_RUNTIME_CLIENT_HOLD: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"AOS_RUNTIME_CLI_HOLD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
