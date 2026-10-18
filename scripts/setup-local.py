"""Install local Codex -> CodeBuddy handoff on Windows without pip or cloud."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This installer currently supports Windows only.")
    checkout = Path(__file__).resolve().parent.parent
    base = Path.home() / ".agentsync"
    base.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if not pythonw.is_file():
        raise SystemExit("A Python installation with pythonw.exe is required.")
    if not (Path.home() / ".codex" / "sessions").is_dir():
        raise SystemExit("No local Codex sessions found. Start a Codex session first.")
    shutil.copytree(checkout / "src" / "agentsync", base / "app" / "agentsync",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    launcher = base / "agentsync-local.py"
    shutil.copyfile(checkout / "scripts" / "local-launcher.py", launcher)
    sys.path.insert(0, str(base / "app"))
    from agentsync.codebuddy import install_hook

    settings = Path.home() / ".codebuddy" / "settings.json"
    if settings.exists():
        backup = settings.with_name("settings.agentsync-backup-%s.json" % time.time_ns())
        shutil.copyfile(settings, backup)
    command = subprocess.list2cmdline([str(python), "-X", "utf8", str(launcher), "hook"])
    install_hook(settings, command)
    # Scan once before making the hook available for the user's first session.
    subprocess.run([str(python), "-X", "utf8", str(launcher), "scan"], check=True)
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    startup.mkdir(parents=True, exist_ok=True)
    daemon_command = subprocess.list2cmdline([str(pythonw), "-X", "utf8", str(launcher), "daemon"])
    vbs = startup / "AgentSync.vbs"
    vbs.write_text('CreateObject("WScript.Shell").Run "' + daemon_command.replace('"', '""') + '", 0, False\n',
                   encoding="utf-16")
    subprocess.Popen([str(pythonw), "-X", "utf8", str(launcher), "daemon"],
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({"mode": "local", "settings": str(settings), "database": str(base / "local.db"),
                      "startup": str(vbs), "log": str(base / "daemon.log")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
