"""Reuse the launcher a pane's owner registered for Herdr restart recovery.

A wrapper such as a proxy launcher can register `<launcher> --resume <id>` as a
pane's resume command through Herdr's custom-integration API. Herdr restart
recovery then reopens the pane with that launcher. Panel Fork reads the same
saved binding so a fork starts with the same launcher instead of the bare agent.

Only an exact `[launcher, "--resume", <current session id>]` binding from the
pane's `custom:<agent>` reporter is accepted, so a stale or foreign binding never
selects a command. Anything else falls back to the native agent command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

PLAIN_COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PUBLIC_ALPHABET = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"


def store_path(sessions: Any, socket_path: str) -> Path | None:
    """Locate the saved-state file of the Herdr session that owns this socket."""
    if not socket_path or not isinstance(sessions, list):
        return None
    wanted = os.path.realpath(socket_path)
    for session in sessions:
        if not isinstance(session, dict):
            continue
        socket, directory = session.get("socket_path"), session.get("session_dir")
        if isinstance(socket, str) and isinstance(directory, str) and os.path.realpath(socket) == wanted:
            return Path(directory) / "session.json"
    return None


def public_pane_id(workspace_id: str, number: int) -> str:
    encoded = ""
    while number:
        number, digit = divmod(number - 1, len(PUBLIC_ALPHABET))
        encoded = PUBLIC_ALPHABET[digit] + encoded
    return f"{workspace_id}:p{encoded}"


def saved_pane(data: Any, pane_id: str) -> Mapping[str, Any] | None:
    """Find a saved pane through Herdr's explicit public-number map only."""
    if not isinstance(data, dict) or data.get("version") != 3:
        return None
    for workspace in data.get("workspaces", []):
        if not isinstance(workspace, dict) or not isinstance(workspace.get("id"), str):
            continue
        numbers = workspace.get("public_pane_numbers", {})
        for tab in workspace.get("tabs", []):
            panes = tab.get("panes", {}) if isinstance(tab, dict) else {}
            for internal_id, entry in panes.items() if isinstance(panes, dict) else ():
                number = numbers.get(internal_id) if isinstance(numbers, dict) else None
                if (isinstance(number, int) and not isinstance(number, bool) and number > 0
                        and public_pane_id(workspace["id"], number) == pane_id and isinstance(entry, dict)):
                    return entry
    return None


def launcher_from_binding(entry: Mapping[str, Any] | None, agent: str, session: str) -> str | None:
    binding = entry.get("agent_resume") if entry else None
    if not isinstance(binding, dict) or binding.get("source") != f"custom:{agent}" or binding.get("agent") != agent:
        return None
    argv = binding.get("argv")
    if not isinstance(argv, list) or len(argv) != 3 or argv[1:] != ["--resume", session]:
        return None
    launcher = argv[0]
    if not isinstance(launcher, str) or not PLAIN_COMMAND.fullmatch(launcher) or launcher == agent:
        return None
    return launcher


def registered_launcher(list_sessions: Callable[[], Any], socket_path: str,
                        pane_id: str, agent: str, session: str) -> str | None:
    """Return the custom launcher saved for this exact pane and session, if any."""
    try:
        path = store_path(list_sessions(), socket_path)
        if path is None:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return launcher_from_binding(saved_pane(data, pane_id), agent, session)
