"""Stable Git project identity shared by local and SSH workspaces."""

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Project:
    project_id: str
    remote: str
    root: str
    branch: str
    head: str
    tracked_changes: tuple[str, ...]


def _git(cwd: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        return ""
    return result.stdout.strip()


def normalize_remote(value: str) -> str:
    """Return a credential-free remote identity."""
    value = value.strip()
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return ""
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.groups()
    elif "://" in value:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.port:
            host += ":%d" % parsed.port
        path = parsed.path
    else:
        return ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return ""
    return "%s/%s" % (host.lower(), path)


def resolve_project(cwd: str) -> Project | None:
    """Resolve a Git workspace without writing to it."""
    if not cwd or not Path(cwd).is_dir():
        return None
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    remote = normalize_remote(_git(root, "remote", "get-url", "origin"))
    if remote:
        seed = "remote:" + remote
    else:
        seed = "local:" + str(Path(root).resolve()).replace("\\", "/").lower()
    project_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD") or "DETACHED"
    head = _git(root, "rev-parse", "HEAD")
    tracked_changes = tuple(line for line in
                            _git(root, "status", "--porcelain", "--untracked-files=no").splitlines()
                            if line)
    return Project(project_id, remote, root, branch, head, tracked_changes)
