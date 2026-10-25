#!/usr/bin/env python3
"""Question-only native forks, based on dmangla3/herdr-fork-from-message (MIT)."""
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

from agent_contracts import AGENTS, ENVIRONMENT_KEYS, ContractError, fork_launch, safe_launch_flags
from runtime_registry import PLUGIN_ID, RegistryError, descendant_of, process_start, read_record, state_root, write_record

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
    processes = info.get("foreground_processes", [])
    if not isinstance(processes, list):
        raise ForkError("Pane has no foreground process list")
    candidates = processes
    if agent:
        candidates = [item for item in processes if isinstance(item, dict) and
                      (item.get("name") == agent or (isinstance(item.get("argv"), list) and item["argv"] and Path(item["argv"][0]).name == agent))]
    if len(candidates) != 1:
        raise ForkError("Pane must have exactly one matching foreground agent process")
    process = object_value(candidates[0], "foreground process")
    argv = process.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ForkError("Foreground process has no verifiable argv")
    if agent and Path(argv[0]).name != agent and process.get("name") != agent:
        raise ForkError("Agent metadata does not match the foreground process")
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
                 destination: str = "pane", mode: str = "question", direction: str = "right", focus: bool = True):
        self.client, self.context, self.env = client, context, env
        self.destination, self.mode, self.direction, self.focus = destination, mode, direction, focus
        self.target: dict[str, str] | None = None

    def execute(self) -> ForkResult:
        try:
            return self._execute()
        except (ForkError, ContractError, RegistryError) as exc:
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
        agent = pane.get("agent")
        if agent not in AGENTS:
            raise ForkError("Supported agents: " + ", ".join(AGENTS))
        process = foreground(self.client, source_id, agent)
        cwd = pane.get("foreground_cwd") or pane.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ForkError("Source working directory is unavailable")
        owner = None
        if agent in ("gjc", "omp"):
            owner = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), pane, process["pid"])
            reference = native_id(owner["session_id"])
            if owner.get("session_file"):
                reference = owner["session_file"]
            if self.mode == "question" and owner.get("bridge_version") != 1:
                raise ForkError("Question mode requires the runtime guard bridge")
        else:
            reference = self._native_reference(pane, agent)
        if self.mode == "message" and agent not in ("claude", "codex"):
            raise ForkError("Earlier-message picker supports Claude and Codex only")
        mcp = self._codex_mcp(process["argv"]) if agent == "codex" and self.mode == "question" else []
        environment = {**self.env, **(owner.get("environment", {}) if owner else {})}
        if owner:
            for key in ENVIRONMENT_KEYS[agent]:
                environment.pop(key, None)
            environment.update(owner.get("environment", {}))
        environment["HERDR_QUESTION_STATE_DIR"] = str(state_root(self.env))
        launch = fork_launch(agent, reference, process["argv"], environment, question=self.mode == "question",
                             bridge=ROOT / "scripts/runtime_bridge.ts", omp_overlay=ROOT / "scripts/question-omp.yml",
                             mcp_servers=mcp, runtime=owner)
        expected_child_id = str(uuid.uuid4()) if agent == "claude" else None
        launch_argv = (*launch.argv, "--session-id", expected_child_id) if expected_child_id else launch.argv
        if owner:
            # The new interactive shell has its own startup environment. Remove
            # absent owner roots explicitly before exec, not with empty variables.
            unset = [part for key in ENVIRONMENT_KEYS[agent] if key not in owner.get("environment", {}) for part in ("-u", key)]
            assignments = [f"{key}={value}" for key, value in launch.environment.items()]
            launch_argv = ("env", *unset, *assignments, *launch_argv)
        source_fingerprint = (pane.get("terminal_id"), process["pid"], reference)
        latest = object_value(self.client.json(["pane", "get", source_id]).get("pane"), "source recheck")
        current_process = foreground(self.client, source_id, agent)
        if (latest.get("terminal_id"), current_process["pid"]) != source_fingerprint[:2]:
            raise ForkError("Source process changed during launch preparation")
        if agent in ("gjc", "omp"):
            fresh = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), latest, process["pid"])
            if fresh["session_id"] != owner["session_id"]:
                raise ForkError("Source session switched before fork")
        elif self._native_reference(latest, agent) != reference:
            raise ForkError("Source session switched before fork")
        target = self._create_target(pane, cwd, launch.environment)
        self.target = target
        self._wait_shell(target["pane_id"])
        if agent in ("gjc", "omp"):
            self._start_shell(target["pane_id"], launch_argv)
        else:
            self._start_native(target["pane_id"], agent, launch_argv[1:])
        fork_id = self._verify_child(target["pane_id"], agent, owner["session_id"] if owner else reference, expected_child_id)
        if self.mode == "message":
            self._picker(target["pane_id"], agent)
        self.client.json(["pane", "rename", target["pane_id"], f"Question · {AGENTS[agent]}" if self.mode == "question" else f"Fork · {AGENTS[agent]}"])
        if self.focus:
            self.client.json(["agent", "focus", target["pane_id"]])
        return ForkResult("ready" if fork_id else "started_identity_pending", source_id,
                          owner["session_id"] if owner else reference, target["pane_id"], fork_id,
                          self.destination, target["workspace_id"], target["tab_id"], cwd, agent, launch.restrictions)

    def _native_reference(self, pane: Mapping[str, Any], agent: str) -> str:
        session = pane.get("agent_session")
        if not isinstance(session, dict) or session.get("agent") != agent or session.get("kind") != "id" or session.get("source") != f"herdr:{agent}":
            raise ForkError(f"No trusted {agent} session reference. Install its Herdr integration and restart it.")
        return native_id(session.get("value"))

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
            raise ForkError("Cannot enumerate configured Codex MCP servers; refusing question mode") from exc

    def _create_target(self, source: Mapping[str, Any], cwd: str, environment: Mapping[str, str]) -> dict[str, str]:
        env_args = [part for key, value in environment.items() for part in ("--env", f"{key}={value}")]
        if self.destination == "pane":
            result = self.client.json(["pane", "split", source["pane_id"], "--direction", self.direction,
                                       "--cwd", cwd, *env_args, "--no-focus"])
            pane = object_value(result.get("pane"), "created pane")
            return {"pane_id": pane["pane_id"], "tab_id": pane["tab_id"], "workspace_id": pane["workspace_id"]}
        args = [self.destination, "create", "--cwd", cwd, "--label", "Question fork", *env_args, "--no-focus"]
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
        self.client.json(["agent", "start", f"question-{uuid.uuid4().hex[:10]}", "--kind", agent,
                          "--pane", pane, "--timeout", str(START_SECONDS * 1000), "--", *arguments], timeout=START_SECONDS + 10)

    def _start_shell(self, pane: str, argv: Sequence[str]) -> None:
        # A private POSIX launcher avoids injecting shell syntax into an unknown interactive shell.
        import tempfile
        fd, filename = tempfile.mkstemp(prefix="herdr-question-", suffix=".sh")
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
            if pane.get("agent") == agent:
                process = foreground(self.client, pane_id, agent)
                if self.mode == "question" and not ("--tools" in process["argv"] or "--sandbox" in process["argv"]):
                    raise ForkError("Child process argv does not contain question restrictions")
                if agent in ("gjc", "omp"):
                    try:
                        record = read_record(state_root(self.env), self.env.get("HERDR_SOCKET_PATH", ""), pane, process["pid"])
                        child_id = native_id(record["session_id"])
                        if self.mode == "question" and not record.get("question_ready"):
                            raise ForkError("Child question guard not ready")
                    except RegistryError:
                        time.sleep(POLL_SECONDS)
                        continue
                else:
                    try:
                        child_id = self._native_reference(pane, agent)
                    except ForkError:
                        if agent == "claude" and expected_id and pane.get("agent_status") in ("idle", "done") and expected_id in process["argv"]:
                            return expected_id
                        if agent == "codex" and pane.get("agent_status") == "idle":
                            return None
                        time.sleep(POLL_SECONDS)
                        continue
                if child_id == parent_id:
                    raise ForkError("Child reported the parent session ID instead of an independent fork")
                if pane.get("agent_status") in ("idle", "done"):
                    return child_id
            time.sleep(POLL_SECONDS)
        raise ForkError("Child startup/identity could not be verified before timeout")

    def _picker(self, pane_id: str, agent: str) -> None:
        keys = ["ctrl+t", "esc"] if agent == "codex" else ["esc", "esc"]
        for key in keys:
            self.client.json(["agent", "send-keys", pane_id, key])
            time.sleep(POLL_SECONDS)


def register(client: HerdrClient, env: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    pane_id = env.get("HERDR_PANE_ID", "")
    pane = object_value(client.json(["pane", "get", pane_id]).get("pane"), "owner pane")
    kind = payload.get("agent")
    if kind not in ("gjc", "omp") or pane.get("agent") != kind:
        raise ForkError("Registration agent does not match the pane owner")
    process = foreground(client, pane_id, kind)
    pid = process["pid"]
    if payload.get("pid") != pid or not descendant_of(os.getpid(), pid):
        raise ForkError("Registration must originate inside the exact foreground owner process")
    session = native_id(payload.get("session_id"))
    cwd = str(Path(payload.get("cwd", "")).resolve())
    if cwd != str(Path(pane.get("foreground_cwd") or pane.get("cwd", "")).resolve()):
        raise ForkError("Registration cwd does not match owner cwd")
    safe = {key: payload[key] for key in ("model", "profile", "thinking", "session_file", "bridge_version", "question_ready", "registration_valid") if key in payload}
    environment = payload.get("environment", {})
    if not isinstance(environment, dict) or any(key not in ENVIRONMENT_KEYS[kind] for key in environment):
        raise ForkError("Owner registration contains unsupported environment keys")
    safe["environment"] = {key: value for key, value in environment.items() if isinstance(value, str)}
    session_file = safe.get("session_file")
    if session_file is not None and (not isinstance(session_file, str) or not Path(session_file).is_absolute()):
        raise ForkError("Owner registration transcript must be an absolute path")
    if isinstance(session_file, str) and not Path(session_file).is_file():
        safe.pop("session_file")
    safe.update(agent=kind, pane_id=pane_id, terminal_id=pane["terminal_id"], pid=pid,
                process_start=process_start(pid), session_id=session, cwd=cwd)
    path = write_record(state_root(env), env.get("HERDR_SOCKET_PATH", ""), safe)
    return {"status": "registered", "path": str(path), "session_id": session}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent native question forks in Herdr")
    parser.add_argument("--destination", choices=DESTINATIONS, default="pane")
    parser.add_argument("--mode", choices=("question", "fork", "message"), default="question")
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
        print(f"Question fork failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
