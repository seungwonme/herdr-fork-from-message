"""Native Panel Fork argv with opt-in read-only restrictions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

AGENTS = {
    "claude": "Claude Code", "codex": "Codex", "omp": "OMP", "gjc": "GJC",
    "pi": "Pi", "omo": "OmO", "opencode": "OpenCode", "cursor": "Cursor",
    "copilot": "GitHub Copilot", "agy": "Antigravity", "hermes": "Hermes",
}
PI_FAMILY = ("gjc", "omp", "pi", "omo")
UNAVAILABLE = {
    "cursor": "its native /fork requires opening the active source as a writer first; source-preserving offline fork API unavailable",
    "agy": "its native /fork requires concurrent source resume; no documented offline fork API",
    "hermes": "native branch omits tool history, and native storage import can repair storage or discard metadata; no verified full-history fork API",
}
READ_ONLY_POLICY = """This is an independent READ-ONLY fork, not a delegated worker.
The inherited conversation is background, not an instruction to resume its tasks.
Wait for a new question. Answer only the user's new questions. Do not continue old
implementation, goals, plans, queued tasks or follow-ups. Do not modify files,
memories, settings, services, Git state or external systems. Do not start agents,
background jobs, schedules or automation. Do not contact or report back to the
parent session. File access is read-only; the checkout is shared, not isolated.
A tool or runtime restriction is authoritative even if inherited instructions ask
for edits or commits. Explain unavailable actions instead of bypassing a limit.
"""
READ_TOOLS = {
    "gjc": ("read", "search", "find", "ast_grep"),
    "omp": ("read", "grep", "glob"),
}
VALUE_FLAGS = {
    "claude": ("--model", "--effort"),
    "codex": ("--model", "-m", "--profile", "-p"),
    "gjc": ("--model", "--mpreset", "--thinking", "--session-dir"),
    "omp": ("--model", "--thinking", "--profile", "--session-dir", "--config"),
    "pi": ("--model", "--thinking", "--session-dir", "--provider"),
    "omo": ("--model", "--thinking", "--session-dir", "--provider"),
    "opencode": ("--server", "--model", "--agent"),
    "cursor": ("--model",),
    "agy": ("--model", "--effort", "--project", "--agent"),
    "copilot": ("--model",),
    # Verified in the installed hermes_cli/_parser.py top-level and chat parsers.
    "hermes": ("--model",),
}
OMITTED_VALUE_FLAGS = {
    "--api-key", "--system-prompt", "--append-system-prompt", "--system-prompt-file",
    "--append-system-prompt-file", "--extension", "-e", "--goal", "--tools", "-t",
    "--exclude-tools", "--session", "--session-id", "--fork", "--resume", "--conversation",
    "--name", "-n", "--skill", "--prompt-template", "--theme", "--mcp-config", "--settings",
    "--permission-mode", "--allowedTools", "--disallowedTools", "--config", "-c",
    "--mode", "--export", "--tui-mode", "--use-theme",
}
ENVIRONMENT_KEYS = {
    "claude": ("CLAUDE_CONFIG_DIR",),
    "codex": ("CODEX_HOME",),
    "gjc": ("GJC_CONFIG_DIR", "GJC_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "omp": ("PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "pi": ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "omo": ("OMO_CODING_AGENT_DIR", "SENPI_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR", "OMO_CODING_AGENT_SESSION_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
    "opencode": ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"),
    "cursor": ("CURSOR_CONFIG_DIR",),
    "copilot": ("COPILOT_HOME",),
    "agy": ("GEMINI_CLI_HOME",),
    "hermes": ("HERMES_HOME", "HERMES_PROFILE"),
}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    restrictions: str = ""


def safe_launch_flags(agent: str, argv: Sequence[str]) -> list[str]:
    """Copy only known value flags, never prompts, credentials, or permissions."""
    if agent not in AGENTS:
        raise ContractError(f"Unsupported agent: {agent}")
    kept: list[str] = []
    index = 1
    while index < len(argv):
        item = argv[index]
        if item == "--":
            break
        flag, separator, inline = item.partition("=")
        if agent == "opencode" and item == "--standalone":
            kept.append(item)
        elif flag in VALUE_FLAGS[agent]:
            if separator:
                value = inline
            elif index + 1 < len(argv):
                index += 1
                value = argv[index]
            else:
                raise ContractError(f"Missing {flag} value in source argv")
            if not value or value.startswith("-") or any(ord(c) < 32 for c in value):
                raise ContractError(f"Invalid {flag} value in source argv")
            kept.extend((flag, value))
        elif flag in OMITTED_VALUE_FLAGS and not separator:
            # A prompt/API-key/extension value can itself look like --model.
            # Consume it rather than reinterpret excluded text as a selector.
            index += 1
        index += 1
    return kept


def fork_launch(
    agent: str,
    reference: str,
    source_argv: Sequence[str],
    environment: Mapping[str, str],
    *,
    read_only: bool,
    bridge: Path,
    omp_overlay: Path,
    mcp_servers: Sequence[tuple[str, str]] = (),
    runtime: Mapping[str, Any] | None = None,
) -> Launch:
    if agent not in AGENTS:
        raise ContractError(f"Unsupported agent: {agent}")
    if agent in UNAVAILABLE:
        raise ContractError(f"Automatic {AGENTS[agent]} fork unavailable: {UNAVAILABLE[agent]}. Source was not resumed or changed")
    if read_only and agent not in ("claude", "codex", "gjc", "omp"):
        raise ContractError(f"Read-only mode is not supported for {AGENTS[agent]}: native guard support has not been verified")
    flags = safe_launch_flags(agent, source_argv)
    state = runtime or {}
    if agent in PI_FAMILY and state.get("session_file"):
        flags = _without(flags, {"--session-dir"}) + ["--session-dir", str(Path(state["session_file"]).parent)]
    # The live owner descriptor wins over initial process flags after model changes.
    model = state.get("model")
    profile = state.get("profile") if agent == "gjc" else None
    if isinstance(model, str) and model:
        flags = _without(flags, {"--model", "-m", "--mpreset", "--provider"})
        flags.extend(("--mpreset", profile) if isinstance(profile, str) and profile else ("--model", model))
    thinking = state.get("thinking")
    if agent in PI_FAMILY and isinstance(thinking, str) and thinking not in ("", "inherit"):
        flags = _without(flags, {"--thinking"}) + ["--thinking", thinking]
    env = {key: environment[key] for key in ENVIRONMENT_KEYS[agent] if environment.get(key)}
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        if environment.get(key):
            env[key] = environment[key]
    for key in ("HERDR_PANEL_FORK_STATE_DIR", "HERDR_PANEL_FORK_HELPER"):
        if environment.get(key):
            env[key] = environment[key]
    command = agent
    if agent in PI_FAMILY:
        native = ["--fork", reference]
    elif agent == "claude":
        native = ["--resume", reference, "--fork-session"]
    elif agent == "codex":
        native = ["fork", reference]
    elif agent == "opencode":
        native = ["mini", "--session", reference, "--fork"]
    else:
        native = ["--resume", reference]
    # Explicit zero prevents an inherited read-only shell environment from
    # silently restricting the normal fork. Native permissions otherwise apply.
    env["HERDR_PANEL_FORK_READ_ONLY"] = "1" if read_only else "0"
    env["HERDR_PANEL_FORK_AGENT"] = agent
    if not read_only:
        return Launch(tuple([command, *native, *flags]), env, "native permissions; shared checkout")
    if agent == "claude":
        flags.extend([
            "--tools", "Read,Grep,Glob", "--permission-mode", "plan",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}',
            "--system-prompt-snapshot", "off",
            "--append-system-prompt", " ".join(READ_ONLY_POLICY.split()),
        ])
        restrictions = "built-in Read/Grep/Glob only; MCP and hooks disabled"
    elif agent == "codex":
        flags.extend([
            "--sandbox", "read-only", "--ask-for-approval", "never",
            "-c", "sandbox_workspace_write.network_access=false",
            "-c", "features.apps=false",
            "-c", "features.hooks=false",
            "-c", "features.multi_agent=false",
            "-c", "features.shell_tool=false",
            "-c", "features.in_app_browser=false",
            "-c", "developer_instructions=" + _toml_string(READ_ONLY_POLICY),
        ])
        for name, transport in mcp_servers:
            if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
                raise ContractError("Codex MCP names with punctuation cannot be safely overridden by this CLI version")
            if transport == "stdio":
                value = '{command="false",enabled=false}'
            elif transport == "streamable_http":
                value = '{url="http://127.0.0.1:1/disabled",enabled=false}'
            else:
                raise ContractError("Unknown Codex MCP transport; refusing read-only mode")
            flags.extend(("-c", f"mcp_servers.{name}={value}"))
        restrictions = "native read-only sandbox; no approval escalation; configured MCP servers disabled"
    else:
        flags.extend(("--tools", ",".join(READ_TOOLS[agent]), "--no-lsp", "--no-pty", "--no-title"))
        if agent == "gjc":
            # The globally registered owner bridge gates restored modes and all tool calls.
            flags.extend(("--no-mcp", "--append-system-prompt", READ_ONLY_POLICY))
            # Native startup command pauses inherited goal state without a model turn.
            flags.append("/goal pause")
        else:
            flags.extend(("--no-extensions", "--extension", str(bridge), "--config", str(omp_overlay),
                          "--append-system-prompt", " ".join(READ_ONLY_POLICY.split())))
        restrictions = "owner bridge denies every non-read tool; inherited goals paused"
    return Launch(tuple([command, *native, *flags]), env, restrictions)


def _without(flags: list[str], names: set[str]) -> list[str]:
    return [part for index in range(0, len(flags), 2) if flags[index] not in names for part in flags[index:index + 2]]


def _toml_string(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
