"""Command line entrypoints for Daemon, Cloud, and CodeBuddy integration."""

import argparse
import json
import logging
import os
import sys
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
    sub.add_parser("mcp", help="Serve shared messages to Trae/CodeBuddy over stdio")
    sub.add_parser("mcp-config", help="Print MCP configuration for IDE settings")
    watch_parser = sub.add_parser("watch", help="Register a local project for cloud downloads and native collection")
    watch_parser.add_argument("path", nargs="?", default=".")
    watch_parser.add_argument("--codebuddy-history", default="", help="History directory or auto")
    publish_parser = sub.add_parser("publish", help="Publish a portable message JSON object from stdin")
    publish_parser.add_argument("path", nargs="?", default=".")
    changes_parser = sub.add_parser("messages", help="Read shared messages after a saved cursor")
    changes_parser.add_argument("path", nargs="?", default=".")
    changes_parser.add_argument("--after", type=int, default=0)
    changes_parser.add_argument("--limit", type=int, default=100)
    import_parser = sub.add_parser("import-codebuddy", help="Import a Codex rollout into closed CodeBuddyCN")
    import_parser.add_argument("rollout", type=Path)
    import_parser.add_argument("--history-dir", type=Path)
    import_parser.add_argument("--backup-dir", type=Path, default=Path.home() / ".agentsync/backups")
    import_parser.add_argument("--name", default="")
    import_parser.add_argument("--activate", action="store_true")
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
        from .sources import scan_registered
        print(daemon.scan(db_path, codex_home) + scan_registered(db_path))
    elif args.command == "sync":
        print(json.dumps(daemon.sync(db_path, cloud_url, token)))
    elif args.command == "watch":
        from .sources import register
        print(json.dumps(register(db_path, args.path, args.codebuddy_history), ensure_ascii=True))
    elif args.command == "publish":
        from .messages import publish
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        print(json.dumps(publish(db_path, args.path, **payload), ensure_ascii=True))
    elif args.command == "messages":
        from .mcp import call
        print(json.dumps(call(db_path, "agentsync_messages", {"path": args.path,
                         "after": args.after, "limit": args.limit}), ensure_ascii=True, indent=2))
    elif args.command == "mcp":
        from .mcp import serve
        serve(db_path)
    elif args.command == "mcp-config":
        command_args = ["mcp"] if getattr(sys, "frozen", False) else ["-X", "utf8", "-m", "agentsync.cli", "mcp"]
        print(json.dumps({"mcpServers": {"agentsync": {"command": sys.executable,
              "args": command_args, "env": {"AGENTSYNC_DB": str(Path(db_path).expanduser().resolve()),
              "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}}}}, ensure_ascii=True, indent=2))
    elif args.command == "hook":
        hook_main()
    elif args.command == "install-codebuddy":
        print(install_hook())
    elif args.command == "import-codebuddy":
        from .codebuddy_history import build_snapshot, discover_history, import_snapshot
        snapshot = build_snapshot(args.rollout, args.name)
        history_dir = args.history_dir or discover_history(snapshot["source"]["workspace"])
        result = import_snapshot(snapshot, history_dir, args.backup_dir, args.activate)
        print(json.dumps(result, ensure_ascii=True, indent=2))
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
