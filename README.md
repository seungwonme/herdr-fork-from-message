# Fork from Message for Herdr

[![CI](https://github.com/dmangla3/herdr-fork-from-message/actions/workflows/ci.yml/badge.svg)](https://github.com/dmangla3/herdr-fork-from-message/actions/workflows/ci.yml)

Fork the Codex or Claude Code conversation in the focused [Herdr](https://herdr.dev)
pane, choose an earlier user message with the agent's native picker, and continue in a
new tab, split pane, or workspace.

The source conversation stays open and unchanged. The plugin uses Herdr's trusted
session metadata and each agent's native fork command; it never edits transcript files
or silently substitutes a fresh conversation.

## Features

- Supports Codex and Claude Code.
- Forks the exact session reported by Herdr's agent integration.
- Preserves the focused pane's working directory.
- Opens the agent's native earlier-message picker in the new destination.
- Cleans up only the incomplete destination when launch or picker detection fails.
- Runs locally with no telemetry, analytics, uploads, or developer callbacks.

## Requirements

| Dependency | Supported version |
| --- | --- |
| Herdr | 0.8.0 or newer |
| Python | 3.10 or newer |
| Codex CLI | 0.146.1 or a compatible version |
| Claude Code | 2.1.220 or a compatible version |

You need only the agent you use. Claude's fork and Rewind picker can open while logged
out, but continuing the conversation requires a Claude login, subscription, API key, or
supported third-party provider.

## Install

Install directly from GitHub:

```sh
herdr plugin install dmangla3/herdr-fork-from-message
```

Check that Herdr recognizes the plugin and your agent integration:

```sh
herdr plugin list --plugin dmangla3.fork-from-message
herdr integration status
```

If the Codex or Claude integration is missing or outdated, install it and restart that
agent:

```sh
herdr integration install codex
herdr integration install claude
```

## Actions

| Action | Qualified action ID | Destination |
| --- | --- | --- |
| **Fork agent from message** | `dmangla3.fork-from-message.fork` | New tab |
| **Fork agent beside current pane** | `dmangla3.fork-from-message.fork-pane` | Split pane |
| **Fork agent into new workspace** | `dmangla3.fork-from-message.fork-workspace` | New workspace |

Invoke an action from Herdr's action UI, or from the CLI:

```sh
herdr plugin action invoke fork --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-pane --plugin dmangla3.fork-from-message
herdr plugin action invoke fork-workspace --plugin dmangla3.fork-from-message
```

## Optional keybindings

The plugin does not overwrite or reserve keybindings. Check
`~/.config/herdr/config.toml` for conflicts, then add the bindings you want:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "dmangla3.fork-from-message.fork"
description = "Fork agent from message"

[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "dmangla3.fork-from-message.fork-pane"
description = "Fork agent beside current pane"
```

These are Prefix, `F` and Prefix, `Shift+F`. If your prefix is `Cmd+B`, as in the
setup used to develop this plugin, that becomes `Cmd+B`, `F` and `Cmd+B`, `Shift+F`.
The workspace action intentionally has no suggested default binding.

Validate and reload the configuration:

```sh
herdr config check
herdr server reload-config
```

## Use

1. Focus a Herdr pane running the Codex or Claude conversation you want to branch.
2. Invoke one of the three fork actions.
3. Wait for the new destination to open with the native picker.
4. Choose the earlier user message and confirm.

### Codex picker

The plugin opens Codex's transcript with `Ctrl+T`, then presses `Esc` to enter message
selection. In Codex CLI 0.146.1:

- `Esc` or Left selects an older user message.
- Right selects a newer user message.
- Enter confirms the selected message.

Up and Down scroll the transcript; they do not change the selected message.

### Claude picker

The plugin opens Claude Rewind with `Esc`, `Esc`. Use Up and Down to choose a prompt,
then Enter. Choose **Restore conversation** to rewind only the forked conversation while
leaving files unchanged.

Claude also offers code-restoration choices. Those choices can modify files in the
shared working directory, so the plugin deliberately leaves that decision to you.

## How it works

```text
focused pane
  -> Herdr trusted agent + session ID
  -> new tab, split pane, or workspace in the same cwd
  -> codex fork <session-id>
     or claude --resume <session-id> --fork-session
  -> native earlier-message picker
```

The split action chooses a right split for a wide pane and a downward split for a
narrow or tall pane. This avoids creating unusably narrow columns.

## Safety and privacy

- The source pane is never resumed, sent keys, rewritten, or closed.
- Session IDs are passed as process arguments, not interpolated into shell commands.
- The plugin does not read, rewrite, truncate, or delete agent JSONL/session files.
- The plugin does not make network requests or send conversation data anywhere.
- A forked Codex or Claude process may contact its normal provider after you submit a
  new prompt; that is the agent's normal behavior, not plugin telemetry.
- Detailed errors stay in Herdr's local plugin log.

## Troubleshooting

Inspect the latest local action errors:

```sh
herdr plugin log list --plugin dmangla3.fork-from-message --limit 5
```

Common failures are explicit:

- **Unsupported pane:** focus a pane running Codex or Claude.
- **Missing session ID:** update the matching Herdr integration and restart the agent.
- **Hook trust review:** review the hooks in Codex, then invoke the action again.
- **Picker timeout:** the installed agent version may have changed its native controls.
- **Launch failure:** confirm `codex` or `claude` is available in Herdr's environment.

When failure happens after destination creation, the plugin closes only that incomplete
pane, tab, or workspace. It never falls back to a fresh session while claiming success.
An agent may retain its own native fork record after a late failure; the plugin does not
delete agent-owned session data.

## Known limitations

- Codex CLI 0.146.1 has no supported API for selecting an exact earlier message. The
  final selection therefore remains in Codex's native transcript UI.
- Codex may not report the new fork's session ID until its first post-fork prompt. The
  plugin verifies a new ID when Herdr reports one and otherwise relies on the successful
  native `codex fork <exact-session-id>` launch plus picker detection.
- Picker detection follows the native controls in the versions listed above. A future
  UI change fails visibly and removes the incomplete destination instead of pretending
  the fork succeeded.
- A non-default `CLAUDE_CONFIG_DIR` is preserved when it is available to the plugin
  process. Herdr does not currently expose variables set only inside the source shell.

## Local development

```sh
git clone https://github.com/dmangla3/herdr-fork-from-message.git
cd herdr-fork-from-message
./scripts/check.sh
herdr plugin link "$PWD"
```

The project uses only Python's standard library. The test harness covers both agents,
all destinations, exact native launch arguments, picker ordering, hostile or missing
session IDs, startup races, timeouts, and destination-specific cleanup.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow and
[SECURITY.md](SECURITY.md) for reporting security issues.

## Uninstall

First remove any matching `[[keys.command]]` blocks from
`~/.config/herdr/config.toml`, then validate and reload the configuration:

```sh
herdr config check
herdr server reload-config
```

For a GitHub installation:

```sh
herdr plugin uninstall dmangla3.fork-from-message
```

For a locally linked checkout:

```sh
herdr plugin unlink dmangla3.fork-from-message
```

Uninstalling the plugin does not remove Codex or Claude session data.

## License

[MIT](LICENSE)
