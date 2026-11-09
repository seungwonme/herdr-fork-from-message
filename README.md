# Panel Fork for Herdr

Personal fork of [dmangla3/herdr-fork-from-message](https://github.com/dmangla3/herdr-fork-from-message), MIT.
One Herdr action opens an independent copy using the agent's native fork operation.
When a plain Codex pane has no reported session ID, it opens the
native conversation picker in the new panel instead. Choose the source there to fork.
Only Claude Code, Codex and OMP are supported. Wrapper launchers for Claude Code
and Codex (for example a proxy launcher) are supported through the contract below.
Any other harness in the pane is rejected before a destination is created.

The parent receives no keystrokes or automatic child replies. Native bookkeeping
can still append fork metadata.
Normal forks use native permissions and tools. Read-only mode is a separate option.
Both panels share files; a conversation fork is **not a Git worktree**.

## Harness compatibility

| Harness / inspected version | Automatic Panel Fork | Requirements / evidence |
| --- | --- | --- |
| Claude Code 2.1.310 | Native | Trusted Herdr ID; explicit new UUID |
| Codex 0.164.0 | Native | Trusted Herdr ID; child identity can remain pending until a new prompt |
| OMP 18.16.0 | Native launch contract | Fresh owner bridge; installed CLI flags checked, current provider E2E unverified |
| Claude Code / Codex through a wrapper launcher | Native via the registered launcher | Exact saved `custom:<agent>` binding; see the wrapper launcher contract |

Every other harness is unsupported. It creates no destination and receives no input.
Same-ID resume is never a substitute for a fork. See the limitations under Verification.

## Actions

| Action | Qualified ID | Behavior |
| --- | --- | --- |
| Fork pane | `seungwonme.panel-fork.fork` | Native current-point fork in a new panel, default |
| Fork pane down | `seungwonme.panel-fork.fork-down` | Native fork below |
| Fork into new tab | `seungwonme.panel-fork.fork-tab` | Native fork in a new tab |
| Fork read-only | `seungwonme.panel-fork.read-only` | Optional restricted fork, Claude/Codex/OMP only |
| Fork from message | `seungwonme.panel-fork.from-message` | Historical picker, Claude/Codex only |

Herdr 0.9.5's pane context menu does **not** list plugin actions. Right-click
passthrough does not invoke the plugin. Use a configured shortcut while the source
pane is focused, or the CLI. Do not create an empty pane first.

The approved local setup uses **Ctrl+B, release, then F** (`prefix+f` with the
default prefix). The plugin never changes keybindings. After checking conflicts,
the corresponding `~/.config/herdr/config.toml` entry is:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "seungwonme.panel-fork.fork"
description = "Fork pane"
```

Validate with `herdr config check`, then `herdr --session default server reload-config`.
No restart is required. A shortcut change does not load bridges into existing agents.

### When the session ID is missing

The normal action can open `codex fork` without a source ID. This is
only available for plain `codex` processes without launch arguments,
additional foreground processes, or a registered custom launcher. Existing proxy
and configured launch paths still require their trusted session binding.

The new panel is labelled **select session**, and the result is
`awaiting_selection`, not a completed fork. Select the intended conversation in
the native picker. No conversation is chosen automatically, no keys are sent to
the source, and neither the normal CLI commands nor global hooks are changed.
In Codex, use Enter to fork; its Esc action starts a new conversation instead.

Malformed or foreign session references still fail closed. Read-only and historical
message modes also require a trusted source ID and do not use this fallback.
Claude Code keeps its existing direct-fork behavior and trusted identity requirement.

```sh
herdr --session default plugin action invoke fork --plugin seungwonme.panel-fork
```

Automation must select the server and pane explicitly:

```sh
python3 scripts/fork_from_message.py --session default --pane <pane-id> --no-focus
```

## Installation and identity

Requires Herdr 0.9.5+, Python 3.10+, and the selected agent CLI. Tests also require
Bun and Biome. No runtime SDK packages, service, provider key copying or remote
callback endpoint are installed.

```sh
git clone https://github.com/seungwonme/herdr-fork-from-message.git ~/.agents/plugins/herdr-fork-from-message
herdr --session default plugin link ~/.agents/plugins/herdr-fork-from-message
```

Claude and Codex need their matching Herdr integration enabled
in the **parent**. Do not disable parent hooks before identity is reported.
Every Claude fork receives an explicit new `--session-id`; only its optional
read-only child disables hooks.

### OMP owner bridge

OMP sources require the plugin bridge, even when OMP reports a Herdr
path. The private owner descriptor additionally proves live model/thinking/settings
and configuration roots. It binds the exact session, transcript path, cwd,
PID/process start, server socket and terminal. Owner-only records expire after
20 seconds and refresh every five seconds. No log globbing, recent-file guessing,
process-environment scraping or transcript rewriting is used.

Startup registration waits up to ten seconds for the native pane label or
foreground owner to become visible, retrying on the existing heartbeat. A
persistent readiness failure warns once; duplicate/foreign owners and invalid
registration fail immediately with bounded candidate counts and PIDs, not argv.
A helper launched directly by a nested runtime proves its foreground ancestor
and skips registration without changing the parent's record. Inherited owner
markers alone never grant or deny ownership.

For a single new OMP process, add `--extension <absolute-bridge-path>` to its
normal launch command. No global configuration change is required for that process:

```text
~/.agents/plugins/herdr-fork-from-message/scripts/runtime_bridge.ts
```

Before global registration, review the bridge and obtain approval;
append its path to existing extension arrays with the CLI's official configuration
interface, never replace other entries. Start a **new** source process afterward.
Existing processes are not remotely restarted or modified.

State lives at `~/.local/state/herdr/plugins/seungwonme.panel-fork/owners/`, or the
Herdr plugin state directory. `HERDR_PANEL_FORK_STATE_DIR` explicitly relocates it.
Child registration pins that root rather than inheriting an unrelated plugin root.

## Native launches and failure semantics

The source must be idle. Its foreground process and session are rechecked before
launch and its identity is checked after child startup. Native paths are:

```text
claude --resume ID --fork-session --session-id NEW_ID
codex fork ID
omp --fork PATH
```

Wrapper launchers: when the source pane's `custom:claude` or `custom:codex`
reporter saved `LAUNCHER --resume ID` (Claude) or `LAUNCHER resume ID` (Codex) as
its Herdr restart command, the fork starts with that launcher through a shell
launch: `LAUNCHER --resume ID --fork-session --session-id NEW_ID` for Claude and
`LAUNCHER fork ID` for Codex. Only an exact binding for the current session ID with
a plain command name is used; anything else keeps the native agent start. The
contract a wrapper must meet is in the next section.

### Wrapper launcher contract

Any command that starts Claude or Codex on a user's behalf (a proxy or gateway
launcher, an `ssh host codex` wrapper, a container entrypoint) can make Herdr
restart recovery and Panel Fork reopen its panes through the same command. Panel
Fork relies only on Herdr's public custom-integration API plus Herdr's saved
state; it does not know any particular wrapper. The wrapper has to:

1. **Run the agent on the pane's TTY.** Herdr recognises the pane's agent through
   the process on the pane terminal. An agent moved to another TTY, for example
   under `script(1)`, is invisible to Herdr, and once the agent's own Herdr hook
   reports a `herdr:<agent>` session the wrapper's `custom:<agent>` reports stop
   being honoured (`resume_not_accepted`). Credential injection that pipes the
   agent's stdout must therefore be avoided or done without re-terminalling.
2. **Register the resume command in the agent CLI's own shape, from the pane.**
   With `HERDR_ENV=1`, `HERDR_SOCKET_PATH` and `HERDR_PANE_ID` set (Herdr sets them
   in every pane shell):

   ```sh
   herdr pane report-agent --source custom:claude --agent claude --state idle \
     --seq "$(date +%s%N)" "$HERDR_PANE_ID" -- LAUNCHER --resume SESSION_ID
   herdr pane report-agent --source custom:codex --agent codex --state idle \
     --seq "$(date +%s%N)" "$HERDR_PANE_ID" -- LAUNCHER resume THREAD_ID
   ```

   `LAUNCHER` is a plain command name on `PATH` (letters, digits, `.`, `_`, `-`),
   not the agent's own name, and it must pass extra arguments through to the agent
   so that `LAUNCHER --resume ID --fork-session --session-id NEW_ID` and
   `LAUNCHER fork ID` work. Re-register whenever the session ID changes (Claude
   `/clear`, a new Codex thread).
3. **Report the agent's session reference too**, with
   `herdr pane report-agent-session --source herdr:<agent> --agent <agent> --seq N
   --agent-session-id ID PANE`, unless the agent's own Herdr hook already does.
   Panel Fork reads the source session from that reference.
4. **Keep reporting state, then release.** Once a custom reporter holds a pane,
   Herdr shows only the state that reporter sends (`idle`, `working`, `blocked`,
   `unknown`), so mirror the agent's lifecycle, and release when the agent exits
   so the pane returns to Herdr's own detection:

   ```sh
   herdr pane release-agent "$HERDR_PANE_ID" --source custom:codex --agent codex --seq "$(date +%s%N)"
   ```

   Herdr 0.9.5 accepts `release-agent` options only after the pane ID; options
   first fail with `unknown option` and the pane stays claimed.

Known trade-offs: Herdr's API does not expose the saved resume command, so Panel
Fork reads the owning session's `session.json` (format version 3) found through
`herdr session list --json`; another format version disables wrapper forks and
keeps the native start. For Codex the relayed `--profile`/`-p` is dropped because a
registered wrapper is assumed to select its own profile; a wrapper that wants the
pane's profile relayed must read it from its own configuration.

Allowlisted launch settings and live bridge values are relayed, not secret
arguments, inherited prompts or permission-bypass flags. OMP shell launches use
owner-only temporary POSIX scripts with shell-neutral paths. Unknown/ambiguous
foreground identities fail closed. Exact Node process titles are supported
when Node hides argv; owner registration supplies live settings in that case.

A failed/login/trust/approval destination is **preserved**, never closed or answered
automatically. Parent resources are never closed. Failures are not replaced by fresh
conversations. A missing-ID native picker is reported separately as
`awaiting_selection`. Codex may delay its new ID until the first post-fork prompt; this is
`started_identity_pending`, not verified identity. Claude can verify its explicit
UUID from argv when read-only child hooks are disabled.

The optional historical picker sends keys only to the child (Codex Ctrl+T then Esc,
Claude Esc twice). Selection is manual. Claude code-restoration choices can modify
shared files; use conversation-only restoration.

## Optional read-only mode

Normal forks add no tool allowlist, read-only sandbox, question policy, goal pause
or MCP/hook restrictions. Native restored goals or automation can resume.
Only `--mode read-only` or the read-only action applies these controls:

- Claude: built-in `Read/Grep/Glob`, plan permissions, no MCP/hooks, appended policy.
- Codex: native read-only sandbox, no approval escalation; apps/hooks/multi-agent/shell
  feature disabled and transport-valid MCP overrides. Punctuated MCP names fail closed.
- OMP: `read/grep/glob`, explicit guard extension only, goal-disabled child overlay;
  every other tool call is denied, including dynamically introduced names.

The child policy treats inherited work as background and forbids contacting the parent.
These are model-tool restrictions, not an OS filesystem sandbox. Shared files,
trusted runtime extensions, user terminal commands and changed permissions remain
outside that boundary.

## Verification

```sh
./scripts/check.sh
```

Tests cover manifest-to-CLI defaults, argv/policy, owner settings, stale records,
source races, missing/wrong IDs, placements, failure preservation, interpreter and
Node-title identities, unsupported-harness rejection, wrapper launchers, and
navigation/goal controls.

Earlier restricted-mode QA on macOS/Herdr 0.9.5 established OMP distinct
IDs/guard readiness/history, but its configured model returned 403. OMP 18.16.0
CLI flags have been checked locally; current provider behavior is not verified by
these tests. Claude's explicit UUID was observed in earlier QA. Earlier Claude
recall and Codex startup/MCP outcomes are not re-established by this change.

No provider prompts were submitted. Model recall and complete tool-event fidelity
are not claimed.

## Remove

Remove the bridge from extension arrays you configured, preserving other entries,
and start new processes. Then unlink:

```sh
herdr --session default plugin unlink seungwonme.panel-fork
```

The upstream `forkr` plugin can remain installed separately. No transcripts,
credentials, global rules, skills, existing hooks or keybindings are deleted.

## References

- [Base implementation](https://github.com/dmangla3/herdr-fork-from-message).
- [forkr](https://github.com/t4t5/herdr-forkr): direct current-point Herdr actions.
- [OMP native fork](https://github.com/can1357/oh-my-pi/blob/v18.12.6/docs/session-operations-export-share-fork-resume.md#fork).

[MIT license](LICENSE). Upstream attribution retained.
