#!/usr/bin/env python3
"""Live end-to-end tester for Panel Fork against an isolated Herdr server.

Starts a throw-away `herdr --session pfe2e-*` server, drives real agents through
one disposable tab, forks each with scripts/fork_from_message.py and checks that
the child recalls a codeword given only to the source. It makes real model calls
and is therefore not part of scripts/check.sh or CI.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import glob
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Sequence

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import saved_launcher  # noqa: E402

FORK_SCRIPT = SCRIPTS / "fork_from_message.py"
SESSION_PREFIX = "pfe2e-"
# Codex reports idle before its input box accepts text; a prompt sent at once can be dropped.
SETTLE_SECONDS = 4
OK_STATUSES = ("ready", "started_identity_pending")
IDLE = ("idle", "done")
REFUSAL = "에이전트 모드에서 지원하지 않습니다"
HUMAN_ONLY = "skipped: run from a human terminal"
# Environment markers `agents-env` treats as "an agent is running". The wrapper
# launchers refuse to run when it sees one; this tester never clears them.
AGENT_MARKERS = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX",
    "ANTIGRAVITY_CONVERSATION_ID", "AGENTS_ENV_AGENT_MODE", "OMPCODE",
)
SOURCE_PROMPT = "Remember the codeword {codeword}. Reply only OK."
CHILD_PROMPT = "What codeword did I give you earlier? Reply with only the codeword."


@dataclass(frozen=True)
class AgentSpec:
    kind: str
    args: tuple[str, ...] = ()
    launcher: str | None = None


SPECS: dict[str, AgentSpec] = {
    "claude": AgentSpec("claude", ("--model", "haiku")),
    # --no-daemon: Codex hooks otherwise run inside a shared app-server daemon whose Herdr
    # environment is whatever pane first started it, so session reports miss this server.
    "codex": AgentSpec("codex", ("--no-daemon",)),
    "omp": AgentSpec("omp", ("--model", "company-proxy/gpt-6.1-terra:low")),
    "claude-proxy": AgentSpec("claude", ("--model", "haiku"), "claude-proxy"),
    "codex-proxy": AgentSpec("codex", ("--no-daemon",), "codex-proxy"),
}
DEFAULT_AGENTS = tuple(SPECS)


class CaseFailed(Exception):
    pass


class CaseSkipped(Exception):
    pass


@dataclass
class Result:
    name: str
    outcome: str = "fail"
    child_id: str = "-"
    codeword_ok: str = "-"
    seconds: float = 0.0
    detail: str = ""


# ---------------------------------------------------------------- pure helpers


def agent_mode_markers(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    return [key for key in AGENT_MARKERS if environ.get(key)]


def refused_by_agent_guard(screen: str) -> bool:
    return REFUSAL in screen


def excerpt(screen: str, lines: int = 25) -> str:
    kept = [line.rstrip() for line in screen.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _text_parts(content: Any, kinds: tuple[str, ...]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(part.get("text", "") for part in content
                   if isinstance(part, dict) and part.get("type") in kinds and isinstance(part.get("text"), str))


def assistant_texts(kind: str, entries: Sequence[dict[str, Any]]) -> list[str]:
    """Assistant message texts of one agent's transcript, oldest first."""
    texts: list[str] = []
    for entry in entries:
        if kind == "claude" and entry.get("type") == "assistant":
            message = entry.get("message")
            text = _text_parts(message.get("content"), ("text",)) if isinstance(message, dict) else ""
        elif kind == "codex" and entry.get("type") == "response_item":
            payload = entry.get("payload")
            ok = isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "assistant"
            text = _text_parts(payload.get("content"), ("output_text", "text")) if ok else ""
        elif kind == "omp" and entry.get("type") == "message":
            message = entry.get("message")
            ok = isinstance(message, dict) and message.get("role") == "assistant"
            text = _text_parts(message.get("content"), ("text",)) if ok else ""
        else:
            text = ""
        if text.strip():
            texts.append(text.strip())
    return texts


def read_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def settled_line_count(path: Path, quiet: float = 3.0, limit: float = 30.0) -> int:
    """Line count once the agent has stopped appending bookkeeping lines for `quiet` seconds."""
    deadline = time.monotonic() + limit
    count, since = line_count(path), time.monotonic()
    while time.monotonic() < deadline and time.monotonic() - since < quiet:
        time.sleep(0.5)
        latest = line_count(path)
        if latest != count:
            count, since = latest, time.monotonic()
    return count


def render_table(results: Sequence[Result]) -> str:
    header = ("agent", "result", "child id", "codeword ok", "seconds")
    rows = [header] + [(r.name, r.outcome, r.child_id, r.codeword_ok, f"{r.seconds:.1f}") for r in results]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def parse_agent_args(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    overrides: dict[str, tuple[str, ...]] = {}
    for value in values:
        name, separator, rest = value.partition("=")
        if not separator or name not in SPECS:
            raise ValueError(f"--args expects AGENT=ARGS with AGENT in {', '.join(SPECS)}: {value!r}")
        overrides[name] = tuple(shlex.split(rest))
    return overrides


# ---------------------------------------------------------------- live harness


class Harness:
    def __init__(self, herdr: str, keep: bool, prompt_timeout: int, overrides: dict[str, tuple[str, ...]]):
        self.herdr, self.keep, self.prompt_timeout, self.overrides = herdr, keep, prompt_timeout, overrides
        self.session = SESSION_PREFIX + secrets.token_hex(4)
        self.base = Path(tempfile.mkdtemp(prefix="pf-e2e.", dir="/tmp" if os.path.isdir("/tmp") else None))
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.cleaned_env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.server: subprocess.Popen[bytes] | None = None
        self.socket = ""
        self.workspace = ""
        self.slot = ""
        self.case_dirs: list[Path] = []
        self.started = False

    # -- herdr CLI

    def cli(self, *args: str, timeout: float = 30, check: bool = True) -> tuple[int, dict[str, Any] | None, str]:
        proc = subprocess.run([self.herdr, "--session", self.session, *args], capture_output=True, text=True,
                              timeout=timeout, env=self.cleaned_env)
        try:
            value = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            value = None
        if check and (proc.returncode != 0 or not isinstance(value, dict) or "result" not in value):
            error = value.get("error") if isinstance(value, dict) else None
            raise CaseFailed(f"herdr {' '.join(args[:3])} failed: {error or proc.stderr.strip() or proc.stdout.strip()}")
        result = value.get("result") if isinstance(value, dict) else None
        return proc.returncode, result if isinstance(result, dict) else None, proc.stderr

    def pane(self, pane_id: str) -> dict[str, Any]:
        _, result, _ = self.cli("pane", "get", pane_id)
        return (result or {}).get("pane") or {}

    def panes(self) -> list[dict[str, Any]]:
        _, result, _ = self.cli("pane", "list")
        return (result or {}).get("panes") or []

    def screen(self, pane_id: str) -> str:
        try:
            proc = subprocess.run([self.herdr, "--session", self.session, "pane", "read", pane_id, "--source", "recent",
                                   "--lines", "60"], capture_output=True, text=True, timeout=20, env=self.cleaned_env)
            return proc.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def close_pane(self, pane_id: str) -> None:
        subprocess.run([self.herdr, "--session", self.session, "pane", "close", pane_id], capture_output=True,
                       text=True, timeout=20, env=self.cleaned_env)

    # -- server lifecycle

    def start(self, first_dir: Path) -> None:
        if self.session == "default":
            raise RuntimeError("refusing to use the default session")
        self.started = True
        log = (self.base / "server.log").open("wb")
        self.server = subprocess.Popen([self.herdr, "--session", self.session, "server"], stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       start_new_session=True, env=self.cleaned_env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = subprocess.run([self.herdr, "--session", self.session, "workspace", "list"], capture_output=True,
                                   text=True, env=self.cleaned_env)
            if probe.returncode == 0:
                break
            if self.server.poll() is not None:
                raise RuntimeError(f"isolated Herdr server exited early; see {self.base / 'server.log'}")
            time.sleep(0.3)
        else:
            raise RuntimeError("isolated Herdr server did not become ready")
        listing = subprocess.run([self.herdr, "session", "list", "--json"], capture_output=True, text=True,
                                 env=self.cleaned_env)
        for entry in json.loads(listing.stdout).get("sessions", []):
            if entry.get("name") == self.session:
                self.socket = entry["socket_path"]
        if not self.socket:
            raise RuntimeError("isolated Herdr session has no socket in `session list`")
        _, result, _ = self.cli("workspace", "create", "--cwd", str(first_dir), "--label", "panel-fork-e2e",
                                "--env", f"HERDR_PANEL_FORK_STATE_DIR={self.state_dir}", "--no-focus")
        root = (result or {}).get("root_pane") or {}
        self.workspace, self.slot = root["workspace_id"], root["pane_id"]

    def stop(self) -> list[str]:
        problems: list[str] = []
        if not self.started:
            shutil.rmtree(self.base, ignore_errors=True)
            return problems
        if self.keep:
            return [f"kept session {self.session} (socket {self.socket}); stop with: "
                    f"herdr --session {self.session} server stop && herdr session delete {self.session}; "
                    f"temp dir {self.base}"]
        if self.workspace:
            subprocess.run([self.herdr, "--session", self.session, "workspace", "close", self.workspace],
                           capture_output=True, text=True, timeout=20, env=self.cleaned_env)
        subprocess.run([self.herdr, "--session", self.session, "server", "stop"], capture_output=True, text=True,
                       timeout=30, env=self.cleaned_env)
        if self.server is not None:
            try:
                self.server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.server.terminate()
                try:
                    self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.server.kill()
        deleted = False
        for _ in range(10):
            proc = subprocess.run([self.herdr, "session", "delete", self.session], capture_output=True, text=True,
                                  env=self.cleaned_env)
            if proc.returncode == 0:
                deleted = True
                break
            time.sleep(0.5)
        if not deleted:
            problems.append(f"could not delete session {self.session}; remove it with `herdr session delete`")
        listing = subprocess.run([self.herdr, "session", "list", "--json"], capture_output=True, text=True,
                                 env=self.cleaned_env)
        try:
            names = [s.get("name") for s in json.loads(listing.stdout).get("sessions", [])]
        except ValueError:
            names = []
        if self.session in names:
            problems.append(f"session {self.session} is still listed")
        shutil.rmtree(self.base, ignore_errors=True)
        return problems

    # -- agent driving

    def split_slot(self, source: str, cwd: Path) -> str:
        _, result, _ = self.cli("pane", "split", source, "--direction", "right", "--cwd", str(cwd), "--env",
                                f"HERDR_PANEL_FORK_STATE_DIR={self.state_dir}", "--no-focus")
        return ((result or {}).get("pane") or {})["pane_id"]

    def start_agent(self, pane_id: str, name: str, spec: AgentSpec, args: Sequence[str]) -> None:
        if spec.launcher:
            self.start_wrapper(pane_id, spec, args)
            return
        try:
            self.cli("agent", "start", name, "--kind", spec.kind, "--pane", pane_id, "--timeout", "60000", "--",
                     *args, timeout=80)
        except CaseFailed as exc:
            raise CaseFailed(f"{exc}\n--- source screen ---\n{excerpt(self.screen(pane_id))}") from exc
        self.require_idle(pane_id)
        time.sleep(SETTLE_SECONDS)

    def start_wrapper(self, pane_id: str, spec: AgentSpec, args: Sequence[str]) -> None:
        assert spec.launcher
        if shutil.which(spec.launcher) is None:
            raise CaseSkipped(f"skipped: {spec.launcher} is not on PATH")
        self.cli("pane", "run", pane_id, spec.launcher, *args)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            screen = self.screen(pane_id)
            if refused_by_agent_guard(screen):
                raise CaseSkipped(HUMAN_ONLY)
            pane = self.pane(pane_id)
            if pane.get("agent_status") == "blocked":
                raise CaseFailed(f"{spec.launcher} is blocked on a dialog; nothing was answered\n{excerpt(screen)}")
            if pane.get("agent") == spec.kind and pane.get("agent_status") in IDLE and pane.get("agent_session"):
                time.sleep(SETTLE_SECONDS)
                return
            time.sleep(1)
        raise CaseFailed(f"{spec.launcher} did not reach idle in 90s\n{excerpt(self.screen(pane_id))}")

    def require_idle(self, pane_id: str) -> dict[str, Any]:
        pane = self.pane(pane_id)
        if pane.get("agent_status") not in IDLE:
            raise CaseFailed(f"pane {pane_id} is {pane.get('agent_status')}, not idle (a trust or approval dialog is "
                             f"never answered)\n{excerpt(self.screen(pane_id))}")
        return pane

    def prompt(self, pane_id: str, text: str) -> None:
        # Herdr reports `agent_prompt_stalled` when no working state was observed within 5s. A fast
        # turn can finish between its polls, so the prompt may have been answered already; a repeat
        # would add a second turn. Only resend when the screen shows no completed reply.
        for attempt in range(2):
            try:
                _, result, _ = self.cli("agent", "prompt", pane_id, text, "--wait", "--timeout",
                                        str(self.prompt_timeout * 1000), timeout=self.prompt_timeout + 20)
                break
            except CaseFailed as exc:
                if "agent_prompt_stalled" not in str(exc):
                    raise
                if self.answered(pane_id, text):
                    result = {"agent": self.pane(pane_id)}
                    break
                if attempt == 1:
                    raise
                time.sleep(SETTLE_SECONDS)
        status = ((result or {}).get("agent") or {}).get("agent_status")
        if status not in IDLE:
            raise CaseFailed(f"agent ended in state {status!r} after the prompt\n{excerpt(self.screen(pane_id))}")

    def answered(self, pane_id: str, text: str) -> bool:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.pane(pane_id).get("agent_status") in IDLE:
                flat = " ".join(self.screen(pane_id).split())
                position = flat.rfind(" ".join(text.split()))
                if position >= 0:
                    return "Worked for" in flat[position + len(text):]
                return False
            time.sleep(1)
        return False

    def wait_launcher(self, pane_id: str, spec: AgentSpec, session: str) -> None:
        """The wrapper's reporter registers its resume command asynchronously."""
        assert spec.launcher
        def sessions() -> Any:
            proc = subprocess.run([self.herdr, "--session", self.session, "session", "list", "--json"],
                                  capture_output=True, text=True, timeout=15, env=self.cleaned_env)
            return json.loads(proc.stdout).get("sessions")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            found = saved_launcher.registered_launcher(sessions, self.socket, pane_id, spec.kind, session)
            if found == spec.launcher:
                return
            time.sleep(1)
        raise CaseFailed(f"{spec.launcher} never registered its resume command for session {session}")

    def fork(self, pane_id: str) -> tuple[int, str, str]:
        env = dict(self.cleaned_env)
        env.update(HERDR_ENV="1", HERDR_SOCKET_PATH=self.socket, HERDR_BIN_PATH=self.herdr,
                   HERDR_PANEL_FORK_STATE_DIR=str(self.state_dir))
        proc = subprocess.run([sys.executable, str(FORK_SCRIPT), "--session", self.session, "--pane", pane_id,
                               "--no-focus"], capture_output=True, text=True, timeout=180, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    # -- transcripts

    def transcript(self, kind: str, session_ref: str) -> Path | None:
        if kind == "omp":
            path = Path(session_ref)
            if path.is_file():
                return path
            root = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp" / "agent")
            hits = glob.glob(str(root / "sessions" / "*" / f"*_{glob.escape(session_ref)}.jsonl"))
            return Path(hits[0]) if hits else None
        if kind == "claude":
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            hits = glob.glob(str(root / "projects" / "*" / f"{glob.escape(session_ref)}.jsonl"))
        else:
            root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            hits = glob.glob(str(root / "sessions" / "*" / "*" / "*" / f"rollout-*-{glob.escape(session_ref)}.jsonl"))
        return Path(hits[0]) if hits else None

    def codex_fork_child(self, source_id: str, since: float) -> str | None:
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        files = sorted(glob.glob(str(root / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")),
                       key=os.path.getmtime, reverse=True)
        for name in files[:80]:
            if os.path.getmtime(name) < since:
                break
            try:
                with open(name, encoding="utf-8") as handle:
                    first = json.loads(handle.readline())
            except (OSError, ValueError):
                continue
            payload = first.get("payload") if isinstance(first, dict) else None
            if isinstance(payload, dict) and payload.get("forked_from_id") == source_id and payload.get("id"):
                return str(payload["id"])
        return None

    def child_identity(self, kind: str, fork: dict[str, Any], child_pane: str, since: float) -> str:
        if fork.get("fork_session_id"):
            return str(fork["fork_session_id"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            session = self.pane(child_pane).get("agent_session")
            if isinstance(session, dict) and session.get("value"):
                return str(session["value"])
            time.sleep(1)
        if kind == "codex":
            found = self.codex_fork_child(str(fork["source_session_id"]), since)
            if found:
                return found
        raise CaseFailed("child session id never became visible (pane report and transcript lookup both empty)")

    def last_answer(self, kind: str, path: Path) -> str:
        deadline = time.monotonic() + 20
        texts: list[str] = []
        while time.monotonic() < deadline:
            texts = assistant_texts(kind, read_entries(path))
            if texts and texts[-1] != "OK":
                break
            time.sleep(1)
        if not texts:
            raise CaseFailed(f"no assistant message in {path}")
        return texts[-1]

    # -- cases

    def run_agent_case(self, name: str, source_pane: str, case_dir: Path, result: Result) -> list[str]:
        """Returns pane IDs created during the case; the caller closes them."""
        spec = SPECS[name]
        args = self.overrides.get(name, spec.args)
        created = [source_pane]
        codeword = "PF" + secrets.token_hex(4).upper()
        started = time.time()
        self.start_agent(source_pane, f"pfe2e-{name}", spec, args)
        self.prompt(source_pane, SOURCE_PROMPT.format(codeword=codeword))
        source = self.require_idle(source_pane)
        deadline = time.monotonic() + 30
        while not isinstance(source.get("agent_session"), dict) and time.monotonic() < deadline:
            time.sleep(1)
            source = self.require_idle(source_pane)
        session = source.get("agent_session")
        if not isinstance(session, dict) or not session.get("value"):
            raise CaseFailed("source reports no agent session; Herdr integration for this agent is not active")
        source_ref = str(session["value"])
        source_path = self.transcript(spec.kind, source_ref)
        if source_path is None:
            raise CaseFailed(f"source transcript not found for {source_ref}")
        before = settled_line_count(source_path)
        before_lines = source_path.read_text(encoding="utf-8").splitlines()
        if spec.launcher:
            self.wait_launcher(source_pane, spec, source_ref)

        code, out, err = self.fork(source_pane)
        preserved = re.search(r"Destination preserved: (\S+?)\.?\s*$", err.strip())
        if preserved:
            created.append(preserved.group(1))
        if code != 0:
            raise CaseFailed(f"fork exited {code}: {err.strip() or out.strip()}")
        try:
            fork = json.loads(out)
        except ValueError as exc:
            raise CaseFailed(f"fork printed non-JSON output: {out[:300]!r}") from exc
        child_pane = str(fork["pane_id"])
        created.append(child_pane)
        if fork.get("status") not in OK_STATUSES:
            raise CaseFailed(f"fork status {fork.get('status')!r}, expected one of {OK_STATUSES}")
        if child_pane == source_pane or not any(p.get("pane_id") == child_pane for p in self.panes()):
            raise CaseFailed(f"child pane {child_pane} does not exist or equals the source")
        child = self.pane(child_pane)
        if child.get("agent") != spec.kind:
            raise CaseFailed(f"child pane runs {child.get('agent')!r}, expected {spec.kind!r}")
        if str(fork.get("source_session_id")) not in source_ref:
            raise CaseFailed(f"fork reported source {fork.get('source_session_id')}, pane has {source_ref}")

        self.prompt(child_pane, CHILD_PROMPT)
        child_id = self.child_identity(spec.kind, fork, child_pane, started)
        result.child_id = child_id
        if child_id == fork["source_session_id"]:
            raise CaseFailed("child session id equals the source session id")
        child_ref = str((self.pane(child_pane).get("agent_session") or {}).get("value") or child_id)
        child_path = self.transcript(spec.kind, child_ref if spec.kind == "omp" else child_id)
        if child_path is None:
            raise CaseFailed(f"child transcript not found for {child_id}")
        answer = self.last_answer(spec.kind, child_path)
        result.codeword_ok = "yes" if codeword in answer else "NO"
        if codeword not in answer:
            raise CaseFailed(f"child answered {answer[:120]!r}, expected the codeword {codeword}")

        after_session = (self.pane(source_pane).get("agent_session") or {}).get("value")
        if after_session != source_ref:
            raise CaseFailed(f"source session changed from {source_ref} to {after_session}")
        after = line_count(source_path)
        if after != before:
            added = source_path.read_text(encoding="utf-8").splitlines()[len(before_lines):]
            raise CaseFailed(f"source transcript {source_path} gained {after - before} entries ({before} -> {after}): "
                             + " | ".join(line[:240] for line in added))
        result.detail = f"fork status {fork['status']}"
        return created

    def run_negative_case(self, source_pane: str, result: Result) -> list[str]:
        panes_before = {p["pane_id"] for p in self.panes()}
        code, out, err = self.fork(source_pane)
        panes_after = {p["pane_id"] for p in self.panes()}
        if code == 0:
            raise CaseFailed(f"fork of a plain shell pane succeeded: {out.strip()[:200]}")
        if "Panel Fork failed" not in err:
            raise CaseFailed(f"unexpected failure output: {err.strip()[:300]}")
        if panes_after != panes_before:
            raise CaseFailed(f"fork of a plain shell pane created {sorted(panes_after - panes_before)}")
        result.codeword_ok = "n/a"
        result.detail = err.strip().splitlines()[-1][:160]
        return [source_pane, *sorted(panes_after - panes_before)]

    def run_all(self, names: Sequence[str]) -> list[Result]:
        cases = [*names, "negative(shell)"]
        self.case_dirs = [Path(tempfile.mkdtemp(prefix="case.", dir=self.base)) for _ in cases]
        self.start(self.case_dirs[0])
        results: list[Result] = []
        for index, name in enumerate(cases):
            result = Result(name)
            began = time.monotonic()
            created = [self.slot]
            failed = False
            try:
                if name == "negative(shell)":
                    created = self.run_negative_case(self.slot, result)
                else:
                    markers = agent_mode_markers(os.environ) if SPECS[name].launcher else []
                    if markers:
                        raise CaseSkipped(HUMAN_ONLY + f" (agent markers: {', '.join(markers)})")
                    created = self.run_agent_case(name, self.slot, self.case_dirs[index], result)
                result.outcome = "pass"
            except CaseSkipped as exc:
                result.outcome, result.detail = "skipped", str(exc)
            except CaseFailed as exc:
                failed = True
                result.outcome, result.detail = "FAIL", self.with_evidence(str(exc), created)
            except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
                failed = True
                result.outcome, result.detail = "FAIL", self.with_evidence(f"{type(exc).__name__}: {exc}", created)
            result.seconds = time.monotonic() - began
            results.append(result)
            self.advance(created, index, cases, close=not (self.keep and failed))
        return results

    def with_evidence(self, message: str, created: Sequence[str]) -> str:
        known = {p.get("pane_id") for p in self.safe_panes()}
        extra = [f"--- screen of {pane_id} ---\n{excerpt(self.screen(pane_id))}" for pane_id in created[1:]
                 if pane_id in known and pane_id not in message]
        return "\n".join([message, *extra])

    def safe_panes(self) -> list[dict[str, Any]]:
        try:
            return self.panes()
        except (CaseFailed, subprocess.SubprocessError, OSError):
            return []

    def advance(self, created: Sequence[str], index: int, cases: Sequence[str], close: bool = True) -> None:
        """Replace the used panes with one fresh shell slot so the tab never empties.

        With `close=False` (--keep after a failure) the used panes stay for inspection."""
        live = {p.get("pane_id") for p in self.safe_panes()}
        victims = [pane_id for pane_id in dict.fromkeys(created) if pane_id in live]
        if index + 1 < len(cases) and victims:
            self.slot = self.split_slot(victims[0], self.case_dirs[index + 1])
        if close:
            for pane_id in reversed(victims):
                self.close_pane(pane_id)


# ---------------------------------------------------------------- entry point


def interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Panel Fork E2E against an isolated Herdr server (real model calls).")
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help="comma list of: " + ", ".join(SPECS))
    parser.add_argument("--keep", action="store_true", help="leave the isolated server, panes and temp dir for inspection")
    parser.add_argument("--args", action="append", default=[], metavar="AGENT=ARGS",
                        help="replace an agent's launch arguments, for example --args 'claude=--model sonnet'")
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for each model answer")
    args = parser.parse_args(argv)
    names = [name.strip() for name in args.agents.split(",") if name.strip()]
    unknown = [name for name in names if name not in SPECS]
    if not names or unknown:
        parser.error(f"unknown agent(s) {unknown or '(none)'}; choose from {', '.join(SPECS)}")
    try:
        overrides = parse_agent_args(args.args)
    except ValueError as exc:
        parser.error(str(exc))
    herdr = shutil.which("herdr")
    if herdr is None:
        print("herdr is not on PATH", file=sys.stderr)
        return 2
    harness = Harness(herdr, args.keep, args.timeout, overrides)
    signal.signal(signal.SIGTERM, interrupt)
    results: list[Result] = []
    problems: list[str] = []
    try:
        results = harness.run_all(names)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except RuntimeError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        results = [Result("setup", "FAIL", detail=str(exc))]
    finally:
        problems = harness.stop()
    print(render_table(results))
    for result in results:
        if result.detail and result.outcome != "pass":
            print(f"\n[{result.name}] {result.outcome}: {result.detail}")
    for problem in problems:
        print(f"\nnote: {problem}")
    failed = not results or any(r.outcome == "FAIL" for r in results)
    return 1 if failed or any(p.startswith(("could not", "session ")) for p in problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
