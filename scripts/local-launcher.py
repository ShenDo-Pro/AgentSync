"""Installed local-only entry point, independent of the checkout and PYTHONPATH."""

import logging
import os
from pathlib import Path
import sys

base = Path(__file__).resolve().parent
sys.path.insert(0, str(base / "app"))
os.environ["AGENTSYNC_DB"] = str(base / "local.db")
os.environ["AGENTSYNC_CLOUD_URL"] = ""
os.environ["AGENTSYNC_TOKEN"] = ""
os.environ["AGENTSYNC_CODEX_HOME"] = str(Path.home() / ".codex")

if __name__ == "__main__":
    if sys.argv[1:] == ["daemon"]:
        import msvcrt

        # The lock is released by Windows even if the process crashes.
        lock = (base / "daemon.lock").open("a+b")
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            sys.exit(0)
        (base / "daemon.pid").write_text(str(os.getpid()), encoding="ascii")
        from logging.handlers import RotatingFileHandler

        logging.basicConfig(level=logging.INFO,
                            handlers=[RotatingFileHandler(base / "daemon.log",
                                      maxBytes=2_000_000, backupCount=2, encoding="utf-8")],
                            format="%(asctime)s %(levelname)s %(message)s")
        logging.info("AgentSync local daemon started (pid=%s)", os.getpid())
    from agentsync.cli import main

    main()
