#!/usr/bin/env python3
"""Fork the focused Herdr Codex or Claude pane, then open its message picker."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PLUGIN_ID = "i-amgeek.fork-from-message"
ACTION_TITLE = "Fork agent from message"
SUPPORTED_AGENTS = {"codex": "Codex", "claude": "Claude"}
DESTINATIONS = ("tab", "pane", "workspace")
COMMAND_TIMEOUT_SECONDS = 15
START_TIMEOUT_MILLISECONDS = 45_000
START_PROCESS_TIMEOUT_SECONDS = 55
SHELL_READY_TIMEOUT_SECONDS = 8
TRANSCRIPT_TIMEOUT_SECONDS = 8
PICKER_TIMEOUT_SECONDS = 8
POLL_INTERVAL_SECONDS = 0.2
TRANSCRIPT_MARKERS = ("edit prev",)
PICKER_MARKERS = ("edit next", "edit message")
CLAUDE_PICKER_MARKERS = ("restore the code and/or conversation",)
HOOK_REVIEW_MARKER = "hooks need review"


class ForkError(RuntimeError):
    """An actionable workflow failure."""


@dataclass(frozen=True)
class ForkResult:
    source_pane_id: str
    source_session_id: str
    fork_pane_id: str
    fork_session_id: str | None
    destination: str
    workspace_id: str
    tab_id: str
    cwd: str


@dataclass(frozen=True)
class ForkTarget:
    destination: str
    workspace_id: str
    tab_id: str
    pane_id: str


class HerdrClient:
    def __init__(self, executable: str) -> None:
        self.executable = executable

    def _run(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ForkError(f"Herdr executable not found: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ForkError(f"Herdr command timed out: {' '.join(args[:3])}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown Herdr error").strip()
            raise ForkError(f"Herdr command failed ({' '.join(args[:3])}): {detail}") from exc

    def json(self, args: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECONDS) -> Mapping[str, Any]:
        output = self._run(args, timeout).stdout
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ForkError(f"Herdr returned invalid JSON for {' '.join(args[:3])}") from exc
        if not isinstance(value, dict):
            raise ForkError(f"Herdr returned an unexpected response for {' '.join(args[:3])}")
        return value

    def text(self, args: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
        return self._run(args, timeout).stdout


def _object(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ForkError(f"Herdr response is missing {description}")
    return value


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _canonical_uuid(value: Any) -> str | None:
    text = _nonempty_string(value)
    if text is None:
        return None
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == text.lower() else None


def load_invocation_context(env: Mapping[str, str]) -> Mapping[str, Any]:
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        raise ForkError(
            "Herdr did not provide focused-pane context. Invoke this through the installed plugin action."
        )
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForkError("Herdr provided malformed focused-pane context.") from exc
    return _object(context, "plugin invocation context")


def _source_details(
    context: Mapping[str, Any], pane: Mapping[str, Any]
) -> tuple[str, str, str, str, str]:
    pane_id = _nonempty_string(context.get("focused_pane_id"))
    if pane_id is None:
        raise ForkError(
            "No focused pane was available. "
            "Focus a Codex or Claude pane and try again."
        )

    actual_pane_id = _nonempty_string(pane.get("pane_id"))
    if actual_pane_id != pane_id:
        raise ForkError(
            "The focused pane changed before it could be verified. "
            "Focus the Codex or Claude pane and retry."
        )

    agent_kind = _nonempty_string(pane.get("agent"))
    if agent_kind not in SUPPORTED_AGENTS:
        detected = agent_kind or "no recognized agent"
        raise ForkError(
            f"The focused pane is {detected}; only Codex and Claude are supported. "
            "Focus a Codex or Claude pane and retry."
        )
    agent_name = SUPPORTED_AGENTS[agent_kind]

    session = pane.get("agent_session")
    if not isinstance(session, dict):
        raise ForkError(
            f"The focused pane has no trusted {agent_name} session ID from Herdr's integration. "
            f"Run `herdr integration install {agent_kind}`, restart {agent_name}, and retry."
        )
    if (
        session.get("agent") != agent_kind
        or session.get("kind") != "id"
        or session.get("source") != f"herdr:{agent_kind}"
    ):
        raise ForkError(
            f"The focused pane has no trusted {agent_name} session ID from Herdr's integration. "
            f"Run `herdr integration install {agent_kind}`, restart {agent_name}, and retry."
        )
    session_id = _canonical_uuid(session.get("value"))
    if session_id is None:
        raise ForkError(
            f"Herdr's {agent_name} integration reported a missing or invalid session ID. "
            f"Restart {agent_name} and retry."
        )

    workspace_id = _nonempty_string(pane.get("workspace_id"))
    if workspace_id is None or workspace_id != _nonempty_string(context.get("workspace_id")):
        raise ForkError(
            "The focused pane's workspace changed before it could be verified. "
            "Retry the action."
        )

    cwd = (
        _nonempty_string(context.get("focused_pane_cwd"))
        or _nonempty_string(pane.get("foreground_cwd"))
        or _nonempty_string(pane.get("cwd"))
    )
    if cwd is None or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ForkError("The focused agent pane has no usable working directory.")

    return pane_id, session_id, workspace_id, cwd, agent_kind


def _fork_label(context: Mapping[str, Any]) -> str:
    source = _nonempty_string(context.get("tab_label"))
    if source:
        clean = " ".join(source.split())
        return f"Fork · {clean}"[:80]
    return "Agent fork"


class ForkWorkflow:
    def __init__(
        self,
        client: HerdrClient,
        context: Mapping[str, Any],
        codex_home: str | None = None,
        claude_config_dir: str | None = None,
        destination: str = "tab",
    ) -> None:
        if destination not in DESTINATIONS:
            raise ForkError(f"Unsupported fork destination: {destination}")
        self.client = client
        self.context = context
        self.codex_home = codex_home
        self.claude_config_dir = claude_config_dir
        self.destination = destination
        self.created_resource: tuple[str, str] | None = None
        self.cleanup_error: str | None = None

    def execute(self) -> ForkResult:
        try:
            return self._execute()
        except Exception as exc:
            self._cleanup()
            message = (
                str(exc)
                if isinstance(exc, ForkError)
                else f"Unexpected workflow error: {exc}"
            )
            if self.cleanup_error:
                message = f"{message} {self.cleanup_error}"
            raise ForkError(message) from exc

    def _execute(self) -> ForkResult:
        source_pane_id = _nonempty_string(self.context.get("focused_pane_id"))
        if source_pane_id is None:
            raise ForkError(
                "No focused pane was available. Focus a Codex or Claude pane and try again."
            )

        pane_response = self.client.json(["pane", "get", source_pane_id])
        pane = _object(_object(pane_response.get("result"), "pane result").get("pane"), "pane data")
        source_pane_id, source_session_id, workspace_id, cwd, agent_kind = _source_details(
            self.context, pane
        )

        target = self._create_target(
            source_pane_id,
            workspace_id,
            cwd,
            agent_kind,
        )
        fork_pane_id = target.pane_id

        agent_name = f"{agent_kind}_fork_{uuid.uuid4().hex[:12]}"
        self._start_fork_agent(agent_name, fork_pane_id, source_session_id, agent_kind)
        self._fail_if_startup_blocked(fork_pane_id)
        self._open_message_picker(fork_pane_id, agent_kind)
        fork_session_id = self._reported_fork_session(
            fork_pane_id, source_session_id, agent_kind
        )
        self._focus_target(target)

        result = ForkResult(
            source_pane_id=source_pane_id,
            source_session_id=source_session_id,
            fork_pane_id=fork_pane_id,
            fork_session_id=fork_session_id,
            destination=target.destination,
            workspace_id=target.workspace_id,
            tab_id=target.tab_id,
            cwd=cwd,
        )
        self.created_resource = None
        return result

    def _environment_args(self, agent_kind: str) -> list[str]:
        if agent_kind == "codex" and self.codex_home:
            return ["--env", f"CODEX_HOME={self.codex_home}"]
        if agent_kind == "claude" and self.claude_config_dir:
            return ["--env", f"CLAUDE_CONFIG_DIR={self.claude_config_dir}"]
        return []

    def _create_target(
        self,
        source_pane_id: str,
        source_workspace_id: str,
        cwd: str,
        agent_kind: str,
    ) -> ForkTarget:
        if self.destination == "pane":
            return self._create_pane_target(source_pane_id, source_workspace_id, cwd, agent_kind)
        if self.destination == "workspace":
            return self._create_workspace_target(cwd, agent_kind)
        return self._create_tab_target(source_workspace_id, cwd, agent_kind)

    def _create_tab_target(
        self,
        workspace_id: str,
        cwd: str,
        agent_kind: str,
    ) -> ForkTarget:
        args = [
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            cwd,
            "--label",
            _fork_label(self.context),
            *self._environment_args(agent_kind),
            "--no-focus",
        ]
        response = self.client.json(args)
        result = _object(response.get("result"), "tab creation result")
        tab = _object(result.get("tab"), "created tab")
        root_pane = _object(result.get("root_pane"), "created root pane")
        tab_id = _nonempty_string(tab.get("tab_id"))
        if tab_id is not None:
            self.created_resource = ("tab", tab_id)
        pane_id = _nonempty_string(root_pane.get("pane_id"))
        if tab_id is None or pane_id is None:
            raise ForkError("Herdr created a tab but did not return its tab and pane IDs.")
        return ForkTarget("tab", workspace_id, tab_id, pane_id)

    def _split_direction(self, source_pane_id: str) -> str:
        response = self.client.json(["pane", "layout", "--pane", source_pane_id])
        layout = _object(_object(response.get("result"), "layout result").get("layout"), "layout")
        panes = layout.get("panes")
        if not isinstance(panes, list):
            raise ForkError("Herdr did not return source pane geometry.")
        for pane in panes:
            if not isinstance(pane, dict) or pane.get("pane_id") != source_pane_id:
                continue
            rect = pane.get("rect")
            if not isinstance(rect, dict):
                break
            width = rect.get("width")
            height = rect.get("height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return "right" if width >= height * 2 else "down"
            break
        raise ForkError("Herdr did not return usable source pane geometry.")

    def _create_pane_target(
        self,
        source_pane_id: str,
        workspace_id: str,
        cwd: str,
        agent_kind: str,
    ) -> ForkTarget:
        direction = self._split_direction(source_pane_id)
        args = [
            "pane",
            "split",
            source_pane_id,
            "--direction",
            direction,
            "--cwd",
            cwd,
            *self._environment_args(agent_kind),
            "--no-focus",
        ]
        response = self.client.json(args)
        pane = _object(_object(response.get("result"), "pane split result").get("pane"), "pane")
        pane_id = _nonempty_string(pane.get("pane_id"))
        if pane_id is not None:
            self.created_resource = ("pane", pane_id)
        tab_id = _nonempty_string(pane.get("tab_id")) or _nonempty_string(
            self.context.get("tab_id")
        )
        actual_workspace_id = _nonempty_string(pane.get("workspace_id")) or workspace_id
        if pane_id is None or tab_id is None:
            raise ForkError("Herdr split a pane but did not return its pane and tab IDs.")
        return ForkTarget(
            "pane",
            actual_workspace_id,
            tab_id,
            pane_id,
        )

    def _create_workspace_target(self, cwd: str, agent_kind: str) -> ForkTarget:
        args = [
            "workspace",
            "create",
            "--cwd",
            cwd,
            "--label",
            _fork_label(self.context),
            *self._environment_args(agent_kind),
            "--no-focus",
        ]
        response = self.client.json(args)
        result = _object(response.get("result"), "workspace creation result")
        workspace = _object(result.get("workspace"), "created workspace")
        workspace_id = _nonempty_string(workspace.get("workspace_id"))
        if workspace_id is not None:
            self.created_resource = ("workspace", workspace_id)
        tab = _object(result.get("tab"), "created workspace tab")
        root_pane = _object(result.get("root_pane"), "created workspace pane")
        tab_id = _nonempty_string(tab.get("tab_id"))
        pane_id = _nonempty_string(root_pane.get("pane_id"))
        if workspace_id is None or tab_id is None or pane_id is None:
            raise ForkError(
                "Herdr created a workspace but did not return its workspace, tab, and pane IDs."
            )
        return ForkTarget(
            "workspace",
            workspace_id,
            tab_id,
            pane_id,
        )

    def _focus_target(self, target: ForkTarget) -> None:
        if target.destination == "pane":
            self.client.json(["agent", "focus", target.pane_id])
        elif target.destination == "workspace":
            self.client.json(["workspace", "focus", target.workspace_id])
        else:
            self.client.json(["tab", "focus", target.tab_id])

    def _start_fork_agent(
        self,
        agent_name: str,
        pane_id: str,
        source_session_id: str,
        agent_kind: str,
    ) -> None:
        native_args = (
            ["fork", source_session_id]
            if agent_kind == "codex"
            else ["--resume", source_session_id, "--fork-session"]
        )
        args = [
            "agent",
            "start",
            agent_name,
            "--kind",
            agent_kind,
            "--pane",
            pane_id,
            "--timeout",
            str(START_TIMEOUT_MILLISECONDS),
            "--",
            *native_args,
        ]
        deadline = time.monotonic() + SHELL_READY_TIMEOUT_SECONDS
        while True:
            try:
                self.client.json(args, timeout=START_PROCESS_TIMEOUT_SECONDS)
                return
            except ForkError as exc:
                if "agent_pane_busy" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_INTERVAL_SECONDS)

    def _fail_if_startup_blocked(self, pane_id: str) -> None:
        screen = self.client.text(
            ["agent", "read", pane_id, "--source", "detection", "--lines", "40"]
        )
        if HOOK_REVIEW_MARKER in screen.lower():
            raise ForkError(
                "Codex is waiting for hook trust review. Review the pending hooks in Codex, "
                "then retry."
            )

    def _reported_fork_session(
        self,
        pane_id: str,
        source_session_id: str,
        agent_kind: str,
    ) -> str | None:
        response = self.client.json(["agent", "get", pane_id])
        agent = _object(_object(response.get("result"), "agent result").get("agent"), "agent data")
        session = agent.get("agent_session")
        if not isinstance(session, dict):
            return None
        fork_session_id = _canonical_uuid(session.get("value"))
        if (
            session.get("agent") != agent_kind
            or session.get("kind") != "id"
            or session.get("source") != f"herdr:{agent_kind}"
            or fork_session_id is None
        ):
            raise ForkError("Herdr reported invalid session metadata for the new agent fork.")
        if fork_session_id == source_session_id:
            raise ForkError(
                "Herdr reported the source session ID for the new pane instead of a fork ID."
            )
        return fork_session_id

    def _wait_for_markers(
        self,
        pane_id: str,
        markers: tuple[str, ...],
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            screen = self.client.text(
                ["agent", "read", pane_id, "--source", "detection", "--lines", "120"]
            )
            lowered = screen.lower()
            if any(marker in lowered for marker in markers):
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return False

    def _wait_for_transcript(self, pane_id: str) -> None:
        if self._wait_for_markers(pane_id, TRANSCRIPT_MARKERS, TRANSCRIPT_TIMEOUT_SECONDS):
            return
        raise ForkError(
            "The Codex fork started, but its native transcript did not appear after Ctrl+T. "
            "Check whether Codex remapped the transcript shortcut."
        )

    def _wait_for_codex_picker(self, pane_id: str) -> None:
        if self._wait_for_markers(pane_id, PICKER_MARKERS, PICKER_TIMEOUT_SECONDS):
            return
        raise ForkError(
            "The Codex transcript opened, but message selection did not appear after Esc. "
            "Check whether Codex changed its backtrack controls."
        )

    def _wait_for_claude_picker(self, pane_id: str) -> None:
        if self._wait_for_markers(pane_id, CLAUDE_PICKER_MARKERS, PICKER_TIMEOUT_SECONDS):
            return
        raise ForkError(
            "The Claude fork started, but its Claude Rewind picker did not appear after Esc Esc. "
            "Confirm the source session has at least one prompt "
            "and check whether Claude changed its rewind controls."
        )

    def _open_message_picker(self, pane_id: str, agent_kind: str) -> None:
        if agent_kind == "codex":
            self.client.json(["agent", "send-keys", pane_id, "ctrl+t"])
            self._wait_for_transcript(pane_id)
            self.client.json(["agent", "send-keys", pane_id, "esc"])
            self._wait_for_codex_picker(pane_id)
            return
        self.client.json(["agent", "send-keys", pane_id, "esc", "esc"])
        self._wait_for_claude_picker(pane_id)

    def _cleanup(self) -> None:
        resource = self.created_resource
        if resource is None:
            return
        kind, resource_id = resource
        try:
            self.client.json([kind, "close", resource_id])
        except Exception as exc:  # Preserve the original failure while surfacing cleanup trouble.
            self.cleanup_error = (
                f"Could not remove incomplete {kind} {resource_id}: {exc}. "
                f"Close that {kind} manually."
            )
        finally:
            self.created_resource = None


def _herdr_executable(env: Mapping[str, str]) -> str:
    configured = _nonempty_string(env.get("HERDR_BIN_PATH"))
    if configured:
        return configured
    discovered = shutil.which("herdr")
    if discovered:
        return discovered
    raise ForkError("Herdr is not available in PATH and HERDR_BIN_PATH was not provided.")


def _notify_failure(client: HerdrClient, message: str) -> None:
    body = f"{message} Logs: herdr plugin log list --plugin {PLUGIN_ID} --limit 5"
    try:
        client.json(
            [
                "notification",
                "show",
                f"{ACTION_TITLE} failed",
                "--body",
                body,
                "--sound",
                "request",
            ]
        )
    except Exception:
        pass


def run(env: Mapping[str, str], destination: str = "tab") -> ForkResult:
    if env.get("HERDR_ENV") != "1":
        raise ForkError("This action must run inside a Herdr-managed session.")
    client = HerdrClient(_herdr_executable(env))
    try:
        context = load_invocation_context(env)
        codex_home = _nonempty_string(env.get("CODEX_HOME"))
        if codex_home and (not Path(codex_home).is_absolute() or not Path(codex_home).is_dir()):
            raise ForkError("CODEX_HOME is set but is not an existing absolute directory.")
        claude_config_dir = _nonempty_string(env.get("CLAUDE_CONFIG_DIR"))
        if claude_config_dir and (
            not Path(claude_config_dir).is_absolute() or not Path(claude_config_dir).is_dir()
        ):
            raise ForkError("CLAUDE_CONFIG_DIR is set but is not an existing absolute directory.")
        return ForkWorkflow(
            client,
            context,
            codex_home=codex_home,
            claude_config_dir=claude_config_dir,
            destination=destination,
        ).execute()
    except ForkError as exc:
        _notify_failure(client, str(exc))
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=ACTION_TITLE)
    parser.add_argument("--destination", choices=DESTINATIONS, default="tab")
    args = parser.parse_args(argv)
    try:
        result = run(os.environ, destination=args.destination)
    except ForkError as exc:
        print(f"{ACTION_TITLE} failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "tab_id": result.tab_id,
                "pane_id": result.fork_pane_id,
                "source_session_id": result.source_session_id,
                "fork_session_id": result.fork_session_id,
                "destination": result.destination,
                "workspace_id": result.workspace_id,
                "cwd": result.cwd,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
