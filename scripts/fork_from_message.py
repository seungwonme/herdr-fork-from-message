#!/usr/bin/env python3
"""Native Panel Fork, based on dmangla3/herdr-fork-from-message (MIT)."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import uuid

from agent_contracts import AGENTS, ENVIRONMENT_KEYS, PI_FAMILY, UNAVAILABLE, ContractError, fork_launch, safe_launch_flags
from harness_identity import ForegroundNotReady, candidate_diagnostics, entrypoint_argv, process_product, select_process, session_id, session_reference
from native_sessions import NativeSessionError, copilot_fork
from runtime_registry import PLUGIN_ID, RegistryError, process_start, read_record, state_root, write_record
from saved_launcher import registered_launcher

ROOT = Path(__file__).resolve().parents[1]
DESTINATIONS = ("pane", "tab", "workspace")
START_SECONDS = 45
POLL_SECONDS = 0.2


class ForkError(RuntimeError):
    pass


class HerdrClient:
    def __init__(self, executable: str, session: str | None = None):
        self.prefix = [executable, *(["--session", session] if session else [])]

    def json(self, args: Sequence[str], timeout: float = 15) -> dict[str, Any]:
        try:
            p = subprocess.run([*self.prefix, *args], capture_output=True, text=True, check=True, timeout=timeout)
            if not p.stdout.strip() and list(args[:2]) == ["pane", "run"]:
                return {}
            value = json.loads(p.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            detail = (getattr(exc, "stderr", "") or str(exc)).strip()
            raise ForkError(f"Herdr {' '.join(args[:2])} failed: {detail}") from exc
        if not isinstance(value, dict):
            raise ForkError("Herdr returned non-object JSON")
        result = value.get("result")
        if not isinstance(result, dict):
            raise ForkError(f"Herdr rejected {' '.join(args[:2])}: {value.get('error', 'no result')}")
        return result

    def sessions(self) -> Any:
        """Herdr's own session list, which reports each session's directory and socket."""
        try:
            p = subprocess.run([*self.prefix, "session", "list", "--json"], capture_output=True, text=True, check=True, timeout=15)
            return json.loads(p.stdout).get("sessions")
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
            raise ForkError("Herdr session list failed") from exc


def object_value(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForkError(f"Missing {name}")
    return value


def native_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ForkError("Missing native session ID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ForkError("Invalid native session ID") from exc
    if str(parsed) != value.lower():
        raise ForkError("Noncanonical native session ID")
    return value


def foreground(client: HerdrClient, pane_id: str, agent: str | None = None) -> dict[str, Any]:
    info = object_value(client.json(["pane", "process-info", "--pane", pane_id]).get("process_info"), "process info")
    processes = info.get("foreground_processes")
    if not isinstance(processes, list) or any(not isinstance(p, dict) for p in processes):
        raise ForkError("Pane has no foreground process list")
    product, process = select_process(processes, agent)
    argv = process.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ForkError("Foreground process has no verifiable argv")
    process["product"] = product
    return process


@dataclass(frozen=True)
class ForkResult:
    status: str
    source_pane_id: str
    source_session_id: str
    pane_id: str
    fork_session_id: str | None
    destination: str
    workspace_id: str
    tab_id: str
    cwd: str
    agent: str
    restrictions: str


class ForkWorkflow:
    def __init__(self, client: HerdrClient, context: Mapping[str, Any], env: Mapping[str, str],
                 destination: str = "pane", mode: str = "fork", direction: str = "right", focus: bool = True):
        self.client, self.context, self.env = client, context, env
        self.destination, self.mode, self.direction, self.focus = destination, mode, direction, focus
        self.target: dict[str, str] | None = None

    def execute(self) -> ForkResult:
        try:
            return self._execute()
        except (ForkError, ContractError, RegistryError, NativeSessionError) as exc:
            # A live process or approval dialog may exist. Preserve it, never close on error.
            suffix = f" Destination preserved: {self.target['pane_id']}." if self.target else ""
            raise ForkError(str(exc) + suffix) from exc

    def _execute(self) -> ForkResult:
        source_id = self.context.get("focused_pane_id")
        if not isinstance(source_id, str) or not source_id:
            raise ForkError("No explicit source pane")
        pane = object_value(self.client.json(["pane", "get", source_id]).get("pane"), "source pane")
        if pane.get("pane_id") != source_id or pane.get("workspace_id") != self.context.get("workspace_id"):
            raise ForkError("Source pane/workspace identity changed")
        reported_agent = pane.get("agent")
        if reported_agent and reported_agent not in AGENTS and reported_agent not in ("antigravity_cli", "antigravity-cli", "cursor-agent"):
            raise ForkError("Supported agents: " + ", ".join(AGENTS))
        process = foreground(self.client, source_id, reported_agent)
        agent = process["product"]
        if agent not in AGENTS:
            raise ForkError("Supported agents: " + ", ".join(AGENTS))
        pane = {**pane, "agent": agent}
        if agent in UNAVAILABLE:
            raise ForkError(f"Automatic {AGENTS[agent]} fork unavailable: {UNAVAILABLE[agent]}. Source was not resumed or changed")
        if pane.get("agent_status") not in ("idle", "done"):
            raise ForkError("Source must be idle before forking; no input was sent")
        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ForkError("Source working directory is unavailable")
        owner = None
        if agent in PI_FAMILY:
            owner = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), pane, process["pid"])
            reference = owner.get("session_file") or native_id(owner["session_id"])
            if self.mode == "read-only" and owner.get("bridge_version") != 1:
                raise ForkError("Read-only mode requires the runtime guard bridge")
        else:
            reference = self._native_reference(pane, agent)
        if self.mode == "message" and agent not in ("claude", "codex"):
            raise ForkError("Earlier-message picker supports Claude and Codex only")
        mcp = self._codex_mcp(process["argv"]) if agent == "codex" and self.mode == "read-only" else []
        environment = {**self.env, **(owner.get("environment", {}) if owner else {})}
        if owner:
            for key in ENVIRONMENT_KEYS[agent]:
                environment.pop(key, None)
            environment.update(owner.get("environment", {}))
        environment["HERDR_PANEL_FORK_STATE_DIR"] = str(state_root(self.env))
        source_session_id = owner["session_id"] if owner else session_id(reference)
        launch = fork_launch(agent, reference, entrypoint_argv(process), environment, read_only=self.mode == "read-only",
                             bridge=ROOT / "scripts/runtime_bridge.ts", omp_overlay=ROOT / "scripts/read-only-omp.yml",
                             mcp_servers=mcp, runtime=owner)
        expected_child_id = str(uuid.uuid4()) if agent == "claude" else None
        launch_argv = (*launch.argv, "--session-id", expected_child_id) if expected_child_id else launch.argv
        if agent in ("pi", "omo"):
            launch_argv = (*launch_argv, "--extension", str(ROOT / "scripts/runtime_bridge.ts"))
        if owner:
            # The new interactive shell has its own startup environment. Remove
            # absent owner roots explicitly before exec, not with empty variables.
            unset = ["-u", "HERDR_PLUGIN_STATE_DIR", *[part for key in ENVIRONMENT_KEYS[agent] if key not in owner.get("environment", {}) for part in ("-u", key)]]
            assignments = [f"{key}={value}" for key, value in launch.environment.items()]
            launch_argv = ("env", *unset, *assignments, *launch_argv)
        source_fingerprint = (pane.get("terminal_id"), process["pid"], process["argv"])
        latest = object_value(self.client.json(["pane", "get", source_id]).get("pane"), "source recheck")
        current_process = foreground(self.client, source_id, agent)
        if (latest.get("terminal_id"), current_process["pid"], current_process["argv"]) != source_fingerprint:
            raise ForkError("Source process changed during launch preparation")
        if owner:
            fresh = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), {**latest, "agent": agent}, process["pid"])
            if fresh["session_id"] != owner["session_id"]:
                raise ForkError("Source session switched before fork")
        elif self._native_reference(latest, agent) != reference:
            raise ForkError("Source session switched before fork")
        if agent == "copilot":
            expected_child_id = copilot_fork(shutil.which("copilot") or "copilot", source_session_id, cwd=cwd, environment=environment)
            launch_argv = (launch_argv[0], "--resume", expected_child_id, *launch_argv[3:])
        # A wrapper that registered `<launcher> --resume <id>` for restart recovery
        # (for example a proxy launcher) also starts the fork.
        launcher = None
        if agent == "claude":
            launcher = registered_launcher(self.client.sessions, self.env.get("HERDR_SOCKET_PATH", ""),
                                           source_id, agent, source_session_id)
        target = self._create_target(pane, cwd, launch.environment)
        self.target = target
        self._wait_shell(target["pane_id"])
        if agent in PI_FAMILY:
            self._start_shell(target["pane_id"], launch_argv)
        elif launcher:
            self._start_shell(target["pane_id"], (launcher, *launch_argv[1:]))
        else:
            self._start_native(target["pane_id"], agent, launch_argv[1:])
        fork_id = self._verify_child(target["pane_id"], agent, source_session_id, expected_child_id)
        parent = object_value(self.client.json(["pane", "get", source_id]).get("pane"), "source postcheck")
        if owner:
            after = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), {**parent, "agent": agent}, process["pid"])
            unchanged = after["session_id"] == source_session_id
        else:
            unchanged = session_id(self._native_reference(parent, agent)) == source_session_id
        if not unchanged:
            raise ForkError("Source identity changed during fork; destination preserved for inspection")
        if self.mode == "message":
            self._picker(target["pane_id"], agent)
        self.client.json(["pane", "rename", target["pane_id"], f"Panel Fork (read-only) · {AGENTS[agent]}" if self.mode == "read-only" else f"Panel Fork · {AGENTS[agent]}"])
        if self.focus:
            self.client.json(["agent", "focus", target["pane_id"]])
        return ForkResult("ready" if fork_id else "started_identity_pending", source_id,
                          source_session_id, target["pane_id"], fork_id,
                          self.destination, target["workspace_id"], target["tab_id"], cwd, agent, launch.restrictions)

    def _native_reference(self, pane: Mapping[str, Any], agent: str) -> str:
        return session_reference(pane, agent)

    def _codex_mcp(self, argv: list[str]) -> list[tuple[str, str]]:
        try:
            launch_flags = safe_launch_flags("codex", argv)
            profile = [part for index in range(0, len(launch_flags), 2) if launch_flags[index] in ("--profile", "-p") for part in launch_flags[index:index + 2]]
            result = subprocess.run(["codex", *profile, "mcp", "list", "--json"], capture_output=True, text=True, check=True, timeout=15)
            entries = json.loads(result.stdout)
            if not isinstance(entries, list):
                raise ValueError("expected MCP list")
            return [(entry["name"], entry["transport"]["type"]) for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ForkError("Cannot enumerate configured Codex MCP servers; refusing read-only mode") from exc

    def _create_target(self, source: Mapping[str, Any], cwd: str, environment: Mapping[str, str]) -> dict[str, str]:
        env_args = [part for key, value in environment.items() for part in ("--env", f"{key}={value}")]
        if self.destination == "pane":
            result = self.client.json(["pane", "split", source["pane_id"], "--direction", self.direction,
                                       "--cwd", cwd, *env_args, "--no-focus"])
            pane = object_value(result.get("pane"), "created pane")
            return {"pane_id": pane["pane_id"], "tab_id": pane["tab_id"], "workspace_id": pane["workspace_id"]}
        args = [self.destination, "create", "--cwd", cwd, "--label", "Panel Fork", *env_args, "--no-focus"]
        if self.destination == "tab":
            args.extend(("--workspace", source["workspace_id"]))
        result = self.client.json(args)
        pane = object_value(result.get("root_pane"), "created root pane")
        tab = object_value(result.get("tab"), "created tab")
        workspace = object_value(result.get("workspace"), "created workspace") if self.destination == "workspace" else source
        return {"pane_id": pane["pane_id"], "tab_id": tab["tab_id"], "workspace_id": workspace.get("workspace_id", source["workspace_id"])}

    def _wait_shell(self, pane_id: str) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            info = self.client.json(["pane", "process-info", "--pane", pane_id]).get("process_info", {})
            processes = info.get("foreground_processes", [])
            if len(processes) == 1 and (processes[0].get("pid") == info.get("shell_pid")):
                return
            time.sleep(POLL_SECONDS)
        raise ForkError("New pane has not reached its shell prompt")

    def _start_native(self, pane: str, agent: str, arguments: Sequence[str]) -> None:
        self.client.json(["agent", "start", f"panel-fork-{uuid.uuid4().hex[:10]}", "--kind", agent,
                          "--pane", pane, "--timeout", str(START_SECONDS * 1000), "--", *arguments], timeout=START_SECONDS + 10)

    def _start_shell(self, pane: str, argv: Sequence[str]) -> None:
        # A private POSIX launcher avoids injecting shell syntax into an unknown interactive shell.
        import tempfile
        fd, filename = tempfile.mkstemp(prefix="herdr-panel-fork-", suffix=".sh")
        path = Path(filename)
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-" for c in filename):
            os.close(fd)
            path.unlink()
            raise ForkError("Temporary launcher path is not shell-neutral")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("#!/bin/sh\n" + "rm -f -- " + shlex.quote(filename) + "\n" + shlex.join(argv) + "\n")
        self.client.json(["pane", "run", pane, "/bin/sh", filename])

    def _verify_child(self, pane_id: str, agent: str, parent_id: str, expected_id: str | None = None) -> str | None:
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            pane = object_value(self.client.json(["pane", "get", pane_id]).get("pane"), "child pane")
            if pane.get("agent_status") == "blocked":
                raise ForkError("Child requires login, trust, or permission input; no approval was answered")
            if pane.get("agent") == agent or (agent == "omo" and pane.get("agent") == "pi"):
                process = foreground(self.client, pane_id, agent)
                if self.mode == "read-only" and not ("--tools" in process["argv"] or "--sandbox" in process["argv"]):
                    raise ForkError("Child process argv does not contain read-only restrictions")
                if agent in PI_FAMILY:
                    try:
                        record = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), {**pane, "agent": agent}, process["pid"])
                        child_id = native_id(record["session_id"])
                        if self.mode == "read-only" and not record.get("read_only_ready"):
                            raise ForkError("Child read-only guard not ready")
                    except RegistryError:
                        time.sleep(POLL_SECONDS)
                        continue
                else:
                    try:
                        child_id = session_id(self._native_reference(pane, agent))
                    except (ForkError, ContractError):
                        if agent == "claude" and expected_id and pane.get("agent_status") in ("idle", "done") and expected_id in process["argv"]:
                            return expected_id
                        if agent == "codex" and pane.get("agent_status") == "idle":
                            return None
                        time.sleep(POLL_SECONDS)
                        continue
                if child_id == parent_id:
                    raise ForkError("Child reported the parent session ID instead of an independent fork")
                if expected_id and child_id != expected_id:
                    raise ForkError("Child reported a session ID different from the native fork result")
                if pane.get("agent_status") in ("idle", "done"):
                    return child_id
            time.sleep(POLL_SECONDS)
        raise ForkError("Child startup/identity could not be verified before timeout")

    def _picker(self, pane_id: str, agent: str) -> None:
        keys = ["ctrl+t", "esc"] if agent == "codex" else ["esc", "esc"]
        for key in keys:
            self.client.json(["agent", "send-keys", pane_id, key])
            time.sleep(POLL_SECONDS)


def registration_ancestor(pid: int, shell_pid: int) -> int | None:
    if type(shell_pid) is not int or shell_pid <= 1:
        raise ForkError("Registration has no verified pane shell")
    deadline = time.monotonic() + 1.0
    seen: set[int] = set()
    ancestor = None
    current = pid
    while current != shell_pid and current > 1 and current not in seen:
        seen.add(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(seen) > 64:
            break
        try:
            result = subprocess.run(["ps", "-p", str(current), "-o", "ppid=,args="],
                                    capture_output=True, text=True, check=True, timeout=remaining)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ForkError("Registration ancestry is unavailable") from exc
        try:
            parent, command = result.stdout.strip().split(None, 1)
            parent = int(parent)
            # ps prints display arguments, not shell-escaped syntax. Only the
            # entrypoint is evidence; quotes in a prompt are not parse errors.
            argv = command.split(maxsplit=2)
        except ValueError as exc:
            raise ForkError("Registration ancestry is unverifiable") from exc
        if current != pid and process_product({"argv": argv}):
            ancestor = current
        current = parent
    if current != shell_pid:
        raise ForkError("Registration ancestry does not reach the pane shell")
    return ancestor


def register(client: HerdrClient, env: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    pane_id = env.get("HERDR_PANE_ID", "")
    pane = object_value(client.json(["pane", "get", pane_id]).get("pane"), "owner pane")
    if not pane_id or pane.get("pane_id") != pane_id or not isinstance(pane.get("terminal_id"), str) or not pane["terminal_id"]:
        raise ForkError("Registration pane/terminal identity is unavailable or changed")
    kind = payload.get("agent")
    if kind not in PI_FAMILY:
        raise ForkError("Registration requires a verified Pi-family agent")
    requested_pid = payload.get("pid")
    if type(requested_pid) is not int or not 1 < requested_pid <= 2147483647:
        raise ForkError("Registration requires a valid owner PID")
    session = native_id(payload.get("session_id"))
    # Registration is sent directly by the extension, not an arbitrary descendant.
    if os.getppid() != requested_pid:
        raise ForkError("Registration must originate inside the exact foreground owner process")
    info = object_value(client.json(["pane", "process-info", "--pane", pane_id]).get("process_info"), "process info")
    processes = info.get("foreground_processes")
    if not isinstance(processes, list) or any(not isinstance(item, dict) for item in processes):
        raise ForkError("Pane has no foreground process list")
    candidates = [item for item in processes if process_product(item)]
    ancestor = registration_ancestor(requested_pid, info.get("shell_pid"))
    if ancestor is not None:
        return {"status": "ignored", "reason": "nested_non_owner", "diagnostics": candidate_diagnostics(candidates)}
    try:
        _, process = select_process(processes, kind)
    except ForegroundNotReady:
        process = None
    diagnostics = candidate_diagnostics([process] if process else [])
    pane_agent = pane.get("agent")
    matches_pane = pane_agent == kind or (kind == "omo" and pane_agent == "pi")
    if not matches_pane and pane_agent not in (None, ""):
        raise ForkError("Registration agent does not match the pane owner")
    if process is not None:
        pid = process.get("pid")
        if type(pid) is not int or not 1 < pid <= 2147483647:
            raise ForkError(f"Foreground owner has no verifiable PID ({diagnostics})")
        if requested_pid != pid:
            raise ForkError(f"Registration must originate inside the exact foreground owner process ({diagnostics})")
    requested_cwd = payload.get("cwd")
    pane_cwd = pane.get("foreground_cwd") or pane.get("cwd")
    if not isinstance(requested_cwd, str) or not Path(requested_cwd).is_absolute() or not isinstance(pane_cwd, str) or not Path(pane_cwd).is_absolute():
        raise ForkError("Registration requires absolute owner and pane working directories")
    cwd = str(Path(requested_cwd).resolve())
    if cwd != str(Path(pane_cwd).resolve()):
        raise ForkError("Registration cwd does not match owner cwd")
    safe = {key: payload[key] for key in ("model", "profile", "thinking", "session_file", "bridge_version", "read_only_ready", "registration_valid") if key in payload}
    environment = payload.get("environment", {})
    if not isinstance(environment, dict) or any(key not in ENVIRONMENT_KEYS[kind] for key in environment):
        raise ForkError("Owner registration contains unsupported environment keys")
    safe["environment"] = {key: value for key, value in environment.items() if isinstance(value, str)}
    session_file = safe.get("session_file")
    if session_file is not None and (not isinstance(session_file, str) or not Path(session_file).is_absolute()):
        raise ForkError("Owner registration transcript must be an absolute path")
    if isinstance(session_file, str) and not Path(session_file).is_file():
        safe.pop("session_file")
    if process is None:
        return {"status": "not_ready", "reason": "foreground_not_visible", "diagnostics": diagnostics}
    if not matches_pane:
        return {"status": "not_ready", "reason": "pane_owner_not_visible", "diagnostics": diagnostics}
    after_pane = object_value(client.json(["pane", "get", pane_id]).get("pane"), "owner pane")
    after_process = foreground(client, pane_id, kind)
    if (any(after_pane.get(key) != pane.get(key) for key in ("pane_id", "terminal_id", "agent", "cwd", "foreground_cwd"))
            or any(after_process.get(key) != process.get(key) for key in ("pid", "argv", "cwd"))):
        raise ForkError("Registration owner changed before publication")
    safe.update(agent=kind, pane_id=pane_id, terminal_id=pane["terminal_id"], pid=pid,
                process_start=process_start(pid), session_id=session, cwd=cwd)
    path = write_record(state_root(env), env.get("HERDR_SOCKET_PATH", ""), safe)
    return {"status": "registered", "path": str(path), "session_id": session}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Panel Fork: native conversation forks in Herdr")
    parser.add_argument("--destination", choices=DESTINATIONS, default="pane")
    parser.add_argument("--mode", choices=("fork", "read-only", "message"), default="fork")
    parser.add_argument("--direction", choices=("right", "down"), default="right")
    parser.add_argument("--no-focus", action="store_true")
    parser.add_argument("--pane")
    parser.add_argument("--session")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.environ.get("HERDR_ENV") != "1":
            raise ForkError("Run inside Herdr or through an installed Herdr plugin action")
        client = HerdrClient(os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr", args.session)
        if args.register:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ForkError("Registration payload must be an object")
            output = register(client, os.environ, payload)
        else:
            context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
            if args.pane:
                pane = object_value(client.json(["pane", "get", args.pane]).get("pane"), "explicit pane")
                context = {"focused_pane_id": args.pane, "workspace_id": pane["workspace_id"]}
            output = asdict(ForkWorkflow(client, context, os.environ, args.destination, args.mode, args.direction, not args.no_focus).execute())
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (ForkError, RegistryError, ContractError, OSError, ValueError) as exc:
        print(f"Panel Fork failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
