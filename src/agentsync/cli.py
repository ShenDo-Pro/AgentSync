"""Command line entrypoints for Daemon, Cloud, and CodeBuddy integration."""

import argparse
import json
import logging
import os
from pathlib import Path

from . import cloud, daemon
from .codebuddy import handoff, hook_main, install_hook
from .identity import resolve_project
from .storage import connect, history, init_local


def main(argv: list[str] | None = None) -> None:
    """Dispatch AgentSync commands."""
    parser = argparse.ArgumentParser(prog="agentsync")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon")
    sub.add_parser("cloud")
    sub.add_parser("scan")
    sub.add_parser("sync")
    sub.add_parser("hook")
    sub.add_parser("install-codebuddy")
    project_parser = sub.add_parser("project")
    project_parser.add_argument("path", nargs="?", default=".")
    handoff_parser = sub.add_parser("handoff")
    handoff_parser.add_argument("path", nargs="?", default=".")
    handoff_parser.add_argument("--task-id", default="")
    history_parser = sub.add_parser("history")
    history_parser.add_argument("path", nargs="?", default=".")
    history_parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    default_db = "~/.agentsync/cloud.db" if args.command == "cloud" else "~/.agentsync/local.db"
    db_path = os.environ.get("AGENTSYNC_DB", default_db)
    codex_home = Path(os.environ.get("AGENTSYNC_CODEX_HOME", "~/.codex")).expanduser()
    cloud_url = os.environ.get("AGENTSYNC_CLOUD_URL", "")
    token = os.environ.get("AGENTSYNC_TOKEN", "")
    if args.command == "daemon":
        daemon.run(db_path, codex_home, cloud_url, token,
                   float(os.environ.get("AGENTSYNC_INTERVAL", "5")))
    elif args.command == "cloud":
        cloud.serve(db_path, token, os.environ.get("AGENTSYNC_BIND", "127.0.0.1"),
                    int(os.environ.get("AGENTSYNC_PORT", "8765")))
    elif args.command == "scan":
        print(daemon.scan(db_path, codex_home))
    elif args.command == "sync":
        print(daemon.flush(db_path, cloud_url, token))
    elif args.command == "hook":
        hook_main()
    elif args.command == "install-codebuddy":
        print(install_hook())
    elif args.command == "project":
        project = resolve_project(args.path)
        print(json.dumps(project.__dict__ if project else None, ensure_ascii=False))
    elif args.command == "handoff":
        print(json.dumps(handoff(args.path, db_path, cloud_url, token,
                                 args.task_id), ensure_ascii=False, indent=2))
    elif args.command == "history":
        project = resolve_project(args.path)
        if not project:
            raise SystemExit("Not a Git project")
        with connect(db_path) as db:
            init_local(db)
            print(json.dumps({"events": history(db, project.project_id, args.limit)},
                             ensure_ascii=False, indent=2))


def client_main() -> None:
    """Launch the Windows client daemon by default."""
    import sys
    main(sys.argv[1:] or ["daemon"])


def server_main() -> None:
    """Launch the Windows cloud server by default."""
    import sys
    main(sys.argv[1:] or ["cloud"])


if __name__ == "__main__":
    main()
