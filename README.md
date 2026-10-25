# Question Fork for Herdr

Personal fork of [dmangla3/herdr-fork-from-message](https://github.com/dmangla3/herdr-fork-from-message), MIT.
One Herdr action opens an independent question-only copy of the current Claude Code,
Codex, OMP, or GJC conversation. The parent receives no keystrokes or automatic replies.
The default is the current point in the conversation, not an earlier-message picker.

## Actions

| Action | Qualified ID | Behavior |
| --- | --- | --- |
| 질문용 분기 (오른쪽) | `seungwonme.question-fork.question` | Read-only question split |
| 질문용 분기 (아래) | `seungwonme.question-fork.question-down` | Read-only question split below |
| 질문용 분기 (새 탭) | `seungwonme.question-fork.question-tab` | Read-only question tab |
| 일반 분기 (원래 권한 유지) | `seungwonme.question-fork.fork` | Native fork, not read-only |
| 과거 메시지에서 분기 (Claude/Codex) | `seungwonme.question-fork.from-message` | Native historical picker |

Use the focused pane's context menu. No keybindings are installed or overwritten.

```sh
herdr plugin action invoke question --plugin seungwonme.question-fork
```

For automation, select the server and pane explicitly instead of relying on focus:

```sh
python3 scripts/fork_from_message.py --session default --pane <pane-id> --no-focus
```

## Installation

Requires Herdr 0.9.5+, Python 3.10+, and the CLI being forked. Tests additionally need
Bun and Biome. The runtime bridge uses only Node/Bun standard APIs, with no SDK package
installation, service, daemon, provider key copying, or network callbacks.

```sh
git clone https://github.com/seungwonme/herdr-fork-from-message.git ~/.agents/plugins/herdr-fork-from-message
herdr --session default plugin link ~/.agents/plugins/herdr-fork-from-message
```

Claude and Codex need their Herdr integration enabled in the **parent**. Do not turn
off parent hooks before its session identity has been reported. The Claude question
child disables hooks and receives a new explicit `--session-id`.

### GJC and OMP runtime registration

Herdr 0.9.5 does not expose a trusted GJC session reference. The plugin-owned bridge
registers the exact live owner's session ID, transcript path, model, thinking, cwd,
PID/process start, server socket, and terminal ID. Records are owner-only, expire after
20 seconds, and refresh every five seconds inside the agent. There is no log globbing,
mtime selection, process-environment scraping, or transcript rewriting.

Read the bridge and obtain approval before global registration. Append this path to
existing `extensions` arrays with each CLI's official `config get/set` commands; never
replace other extension entries:

```text
~/.agents/plugins/herdr-fork-from-message/scripts/runtime_bridge.ts
```

Start a **new** GJC/OMP process after registration. Existing processes are not remotely
restarted. GJC 0.19.0 can retain a startup manager facade after in-process navigation:
after `/new`, `/resume`, `/fork` or `/tree`, the bridge invalidates its registration.
Restart that process with the desired session before forking again. It refuses stale
identity rather than silently cloning the predecessor.

State lives at `~/.local/state/herdr/plugins/seungwonme.question-fork/owners/`, or the
Herdr plugin state directory. `HERDR_QUESTION_STATE_DIR` explicitly relocates it.

## Question boundaries

- Claude: built-in `Read/Grep/Glob`, plan permissions, no MCP, no hooks, appended policy.
- Codex: native read-only sandbox, no approval escalation, apps/hooks/multi-agent/shell
  feature disabled, configured MCP servers disabled using transport-valid overrides.
  Codex MCP names outside alphanumeric, underscore and hyphen fail closed.
- GJC: `read/search/find/ast_grep`, no MCP/LSP/PTY; owner bridge denies every other tool.
  Native `/goal pause` plus the runtime goal pause control stop inherited goals.
- OMP: `read/grep/glob`, explicit guard extension only, per-child goal-disabled overlay;
  all other tool calls are denied, including dynamically introduced tool names.

Question policy is injected only into the child. It tells the child to wait for new
questions, not resume inherited implementation, and not contact the parent. The checkout
is **shared**, not a Git worktree or OS filesystem sandbox. These controls restrict model
tools, not arbitrary user terminal commands, runtime code, manually changed permissions,
or trusted extensions. Do not describe them as complete process isolation.

### Start and failure semantics

The plugin rechecks the source before creating a destination and uses native fork APIs:

```text
claude --resume ID --fork-session --session-id NEW_ID
codex fork ID
omp --fork PATH
gjc --fork PATH
```

It relays allowlisted launch settings and live bridge model/profile/thinking, not secret
arguments, inherited prompts, arbitrary environment, or permission-bypass flags.
Shell launches use an owner-only temporary POSIX script and shell-neutral path.

A blocked/login/trust/error destination is **preserved**, never automatically closed or
answered. Parent resources are never closed. Failed launches are not replaced by fresh
conversations. Codex can delay reporting its child ID until its first post-fork prompt;
that state is reported as `started_identity_pending`, not verified identity.
Claude uses a new explicit UUID because disabling hooks also disables Herdr ID reporting.

The optional earlier-message picker uses native keyboard controls (Codex Ctrl+T then
Esc; Claude Esc twice). The final selection is manual and shares the checkout. Claude
code-restoration choices can modify files; use conversation-only restoration.

## Verification

```sh
./scripts/check.sh
```

Unit tests cover native argv/policy, allowlisted environment, source-session races,
wrong or missing IDs, startup failures, destination preservation, all placements, stale
owner records, symlinks, unknown tool denial, and goal gate requirements.

Live QA on macOS/Herdr 0.9.5:

- GJC 0.19.0: distinct child ID, inherited token recall, write request refused and probe
  file absent; parent not sent input.
- OMP 18.12.6: distinct child ID, guard-ready registration, inherited transcript. The
  configured Bedrock model returns HTTP 403, so provider answering is not verified.
- Claude Code 2.1.310: explicit child UUID, inherited token recall with hooks disabled.
- Codex 0.164.0: native fork lineage, inherited token recall, distinct child ID after prompt.

No tests send private parent content to a different provider. Fixtures are disposable
conversations created specifically for QA.

## Remove

Remove the bridge path from the two CLI `extensions` arrays, preserving other entries,
and start new processes. Then unlink the plugin:

```sh
herdr --session default plugin unlink seungwonme.question-fork
```

The upstream `forkr` plugin can remain installed as a separate alternative. No session
transcripts, credentials, global rules, skills, existing hooks or keybindings are deleted.

## References

- [Base implementation and tests](https://github.com/dmangla3/herdr-fork-from-message).
- [forkr](https://github.com/t4t5/herdr-forkr): small, direct current-point Herdr actions.
- [OMP native fork placement](https://github.com/can1357/oh-my-pi/blob/v18.12.6/docs/session-operations-export-share-fork-resume.md#fork): parent preservation, launch settings, safe shell execution.

[MIT license](LICENSE). Upstream attribution is retained.
