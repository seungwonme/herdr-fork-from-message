# Panel Fork for Herdr

Personal fork of [dmangla3/herdr-fork-from-message](https://github.com/dmangla3/herdr-fork-from-message), MIT.
One Herdr action opens an independent copy using the agent's native fork operation.
Claude Code, Codex, OMP, GJC, Pi and OMO have native launch paths. OpenCode and
Copilot adapters require the prerequisites below. Cursor, Antigravity and Hermes
are recognized but safely rejected, not silently resumed.

The parent receives no keystrokes or automatic child replies. Native bookkeeping
can still append fork metadata, notably Copilot's source `session.info` fork event.
Normal forks use native permissions and tools. Read-only mode is a separate option.
Both panels share files; a conversation fork is **not a Git worktree**.

## Harness compatibility

| Harness / inspected version | Automatic Panel Fork | Requirements / evidence |
| --- | --- | --- |
| Claude Code 2.1.310 | Native | Trusted Herdr ID; explicit new UUID |
| Codex 0.164.0 | Native | Trusted Herdr ID; child identity can remain pending until a new prompt |
| GJC 0.19.0 | Native | Fresh owner bridge registration |
| OMP 18.12.6 | Native | Fresh owner bridge; configured provider returned 403 in prior QA |
| Pi 1.3.0 | Verified native fixture | Owner bridge; distinct IDs, all fixture entries inherited, parent ID unchanged |
| OMO 5.1.46 | Verified native fixture | Owner bridge; original OMO launcher/config roots/extensions retained, not replaced by Pi |
| OpenCode 2.1.4 | Conditional adapter | `mini --session ID --fork`; installed Herdr integration must report `ses_...` ID. Local integration absent; full panel E2E unverified |
| Copilot CLI 1.0.80 | Conditional adapter | Experimental headless `sessions.fork`; durable source in matching storage root and trusted Herdr integration. Native event survived restart; full TUI/provider E2E unverified |
| Cursor Agent 2026.10.22 | Blocked before destination | `/fork` exists, but source resume can write original metadata; no safe public offline fork API verified |
| Antigravity 1.4.7 | Blocked before destination | Interactive `/fork` exists; no documented offline fork API without concurrent source resume |
| Hermes, local installation | Blocked before destination | Dependency environment unavailable; native branch drops tool history, portability import can repair storage or omit metadata |

Recognition is not a support claim. Blocked harnesses create no destination and
receive no input. Cursor/Antigravity still offer manual `/fork`, which switches the
invoking terminal; this is not the plugin's independent-panel workflow. No Hermes
dependency repair, storage clone or service restart is attempted. Same-ID resume is
never a substitute for a fork. See the limitations under Verification.

## Actions

| Action | Qualified ID | Behavior |
| --- | --- | --- |
| Fork pane | `seungwonme.panel-fork.fork` | Native current-point fork in a new panel, default |
| Fork pane down | `seungwonme.panel-fork.fork-down` | Native fork below |
| Fork into new tab | `seungwonme.panel-fork.fork-tab` | Native fork in a new tab |
| Fork read-only | `seungwonme.panel-fork.read-only` | Optional restricted fork, four original harnesses only |
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

Claude, Codex, OpenCode and Copilot need their matching Herdr integration enabled
in the **parent**. Do not disable parent hooks before identity is reported.
Every Claude fork receives an explicit new `--session-id`; only its optional
read-only child disables hooks. OpenCode/Copilot use configuration roots from the
invoking environment, not an owner snapshot: custom XDG/config roots must match the
source. This equivalence has not been established for arbitrary existing sources.

### GJC, OMP, Pi and OMO owner bridge

All four Pi-family sources require the plugin bridge, even when Pi reports a Herdr
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

GJC also runs task sessions inside the foreground process. The bridge accepts
only native `sessionMetadata.kind: "main"` events; child and unclassified sessions
cannot publish, invalidate, or stop the parent registration. It reads the current
cwd through `sessionManager.getCwd()` rather than the launch-time context, so
worktree relocation does not leave the owner record on the previous directory.
Other Pi-family runtimes keep their own context and process-ownership checks.

For a single new Pi/OMO process, add `--extension <absolute-bridge-path>` to its
normal launch command. No global configuration change is required for that process:

```text
~/.agents/plugins/herdr-fork-from-message/scripts/runtime_bridge.ts
```

The plugin explicitly adds this bridge to Pi/OMO children and preserves OMO's native
launcher and plugin. Before global registration, review the bridge and obtain approval;
append its path to existing extension arrays with the CLI's official configuration
interface, never replace other entries. Start a **new** source process afterward.
Existing processes are not remotely restarted or modified.

GJC 0.19.0 can retain a startup manager facade after `/new`, `/resume`, `/fork` or
`/tree`; the bridge revokes the owner record before admitting navigation instead
of cloning stale identity. Revocation receives up to three attempts; an
unconfirmed tombstone cancels navigation and reports invalidation pending.
A cancelled navigation can resume registration only on a new turn with the
exact unchanged conversation and a confirmed tombstone. A completed switch
requires restarting GJC with the desired session before forking again.

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
gjc --fork PATH
pi --fork PATH
omo --fork PATH
opencode mini --session ID --fork
# Copilot: private sessions.fork({sessionId: ID}), then:
copilot --resume NEW_ID
```

Allowlisted launch settings and live bridge values are relayed, not secret
arguments, inherited prompts or permission-bypass flags. OpenCode `--standalone`
is retained. Pi-family shell launches use owner-only temporary POSIX scripts with
shell-neutral paths. Unknown/ambiguous foreground identities fail closed, including
an OMO process advertised to Herdr as Pi. Exact Node process titles are supported
when Node hides argv; owner registration supplies live settings in that case.

Copilot uses a private headless process with a 30-second operation deadline, bounded
stdio frames, no client tool/auth/permission callbacks and private-process-group
cleanup. It does not resume the parent. The native API adds a source fork-info event;
this is bookkeeping, not a child answer or automatic model turn. An RPC acknowledgement
alone does not prove complete durable history. TUI startup must report the expected
new ID. The native CLI can still perform its own normal authentication/network startup.

A failed/login/trust/approval destination is **preserved**, never closed or answered
automatically. Parent resources are never closed. Failures are not replaced by fresh
conversations. Codex may delay its new ID until the first post-fork prompt; this is
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
- GJC: `read/search/find/ast_grep`, no MCP/LSP/PTY; owner bridge rejects all other tools.
  Native `/goal pause` and runtime goal pause stop inherited goals.
- OMP: `read/grep/glob`, explicit guard extension only, goal-disabled child overlay;
  every other tool call is denied, including dynamically introduced names.

New Pi/OMO/OpenCode/Copilot read-only modes are **unverified and rejected before
creating a destination**. Native fork support does not imply read-only support.
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
Node-title identities, OMO launcher selection, blocked harnesses, navigation/goal
controls, and bounded RPC framing/callbacks/timeouts/private-process cleanup.

Earlier restricted-mode QA on macOS/Herdr 0.9.5 established GJC distinct IDs,
inherited token recall and write refusal; OMP distinct IDs/guard readiness/history
but its Bedrock model returned 403. Claude's explicit UUID was observed. Earlier
Claude recall and Codex startup/MCP outcomes are not re-established by this change.

Additional model-free QA reused one disposable tab with one source and at most one
child at a time. Pi and OMO produced distinct child IDs with equal inherited native
fixture entries and unchanged parent IDs. Copilot's native logged event survived a
fork and a new headless process; its parent gained the native fork notification.
An initial log-only Copilot session without a native fork was not discoverable after
restart, so source persistence is a real prerequisite. OpenCode's isolated native API
rejected an empty-session fork; its missing Herdr integration prevented panel E2E.
It was not installed as part of QA.

No provider prompts were submitted. Model recall, complete tool-event fidelity and
Copilot/OpenCode TUI E2E are not claimed. These fixtures contain no private parent
content. Compatibility diagnostics and static API evidence do not mean all 11
harnesses are fully supported.

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
- [Copilot fork schema](https://github.com/github/copilot-sdk/blob/v0.2.2/nodejs/src/generated/rpc.ts#L394-L410): omit `toEventId` for all events.
- [OpenCode session selection](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/cli/src/session-target.ts).
- [Cursor slash commands](https://cursor.com/docs/cli/reference/slash-commands).
- [Antigravity conversations](https://antigravity.google/docs/cli/conversations).

[MIT license](LICENSE). Upstream attribution retained.
