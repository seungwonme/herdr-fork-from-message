"""Owner-written, private pane bindings. No process environment or log scanning."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

PLUGIN_ID = "seungwonme.question-fork"
MAX_AGE_SECONDS = 20


class RegistryError(ValueError):
    pass


def state_root(env: Mapping[str, str]) -> Path:
    configured = env.get("HERDR_PLUGIN_STATE_DIR") or env.get("HERDR_QUESTION_STATE_DIR")
    if configured:
        path = Path(configured)
    else:
        path = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "herdr/plugins" / PLUGIN_ID
    if not path.is_absolute():
        raise RegistryError("Plugin state directory must be absolute")
    return path


def record_path(root: Path, socket: str, pane: str) -> Path:
    digest = hashlib.sha256((socket + "\0" + pane).encode()).hexdigest()
    return root / "owners" / (digest + ".json")


def process_start(pid: int) -> str:
    p = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=True, timeout=3)
    value = p.stdout.strip()
    if not value:
        raise RegistryError("Owner process no longer exists")
    return value


def descendant_of(pid: int, owner: int) -> bool:
    for _ in range(64):
        if pid == owner:
            return True
        if pid <= 1:
            return False
        p = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True, text=True, timeout=3)
        try:
            pid = int(p.stdout.strip())
        except ValueError:
            return False
    return False


def write_record(root: Path, socket: str, data: Mapping[str, Any]) -> Path:
    directory = root / "owners"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RegistryError("Unsafe owner state directory")
    os.chmod(directory, 0o700)
    destination = record_path(root, socket, str(data["pane_id"]))
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".owner-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({**data, "socket": socket, "updated_at": time.time()}, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def read_record(root: Path, socket: str, pane: Mapping[str, Any], pid: int) -> dict[str, Any]:
    path = record_path(root, socket, str(pane["pane_id"]))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 32768:
                raise RegistryError("Unsafe owner record permissions or size")
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise RegistryError("No fresh owner registration. Enable the plugin runtime bridge and restart this agent.") from exc
    if not isinstance(value, dict):
        raise RegistryError("Invalid owner record")
    if value.get("registration_valid") is False:
        raise RegistryError("Owner session changed; restart the agent before forking")
    checks = {
        "socket": socket, "pane_id": pane["pane_id"], "terminal_id": pane.get("terminal_id"),
        "pid": pid, "process_start": process_start(pid), "agent": pane.get("agent"),
    }
    if any(value.get(key) != expected for key, expected in checks.items()):
        raise RegistryError("Owner registration no longer belongs to this exact pane/process")
    age = time.time() - float(value.get("updated_at", 0))
    if age < -5 or age > MAX_AGE_SECONDS:
        raise RegistryError("Owner registration expired; the runtime bridge is not reporting")
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    if not cwd or Path(value.get("cwd", "")).resolve() != Path(cwd).resolve():
        raise RegistryError("Owner registration working directory changed")
    if not isinstance(value.get("session_id"), str):
        raise RegistryError("Owner registration has no session ID")
    return value
