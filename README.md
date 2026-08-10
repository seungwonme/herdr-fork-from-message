# Herdr: Fork agent from message

A local-only Herdr plugin that forks the Codex or Claude Code session in the focused pane into a new tab, split pane, or workspace, then opens that agent's native earlier-message picker. The original pane is never resumed, rewritten, sent keys, or closed.

## Requirements

- Herdr 0.8.0 or newer
- Codex CLI 0.146.1 or a compatible version with `codex fork <SESSION_ID>` and the native transcript overlay
- Claude Code 2.1.220 or a compatible version with `--resume <SESSION_ID> --fork-session` and native Rewind
- Python 3.10 or newer
- Herdr's Codex and/or Claude integration installed and reporting `agent_session` metadata

Check the integration with:

```sh
herdr integration status
```

Install any missing or outdated integration, then restart that agent:

```sh
herdr integration install codex
herdr integration install claude
```

## Install or link locally

Run the tests, then link this folder:

```sh
./scripts/check.sh
herdr plugin link "$PWD"
herdr plugin action list --plugin local.codex-fork-from-message
```

The plugin exposes three actions:

| Action | Qualified ID | Destination |
| --- | --- | --- |
| **Fork agent from message** | `local.codex-fork-from-message.fork` | New tab |
| **Fork agent beside current pane** | `local.codex-fork-from-message.fork-pane` | Geometry-aware split pane |
| **Fork agent into new workspace** | `local.codex-fork-from-message.fork-workspace` | New Herdr workspace/space |

To add a keybinding, first inspect `~/.config/herdr/config.toml` and `herdr --default-config` for conflicts. Then append a free binding such as:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "local.codex-fork-from-message.fork"
description = "Fork agent from message"
```

An optional side-by-side binding is:

```toml
[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "local.codex-fork-from-message.fork-pane"
description = "Fork agent beside current pane"
```

The workspace action intentionally has no default binding. Invoke it by qualified ID or add a binding if it becomes frequent.

Validate and reload the configuration:

```sh
herdr config check
herdr server reload-config
```

## Use

1. Focus a Herdr pane running the Codex or Claude conversation to branch.
2. Invoke the destination action through its configured keybinding or run one of:

   ```sh
   herdr plugin action invoke local.codex-fork-from-message.fork
   herdr plugin action invoke local.codex-fork-from-message.fork-pane
   herdr plugin action invoke local.codex-fork-from-message.fork-workspace
   ```

3. The action detects the agent, verifies Herdr's exact trusted session ID, and preserves the focused pane's cwd. The tab action creates a labeled tab, the pane action chooses a right split for a wide pane or a downward split for a narrow/tall pane, and the workspace action creates a labeled Herdr workspace.
4. For Codex, it runs `codex fork <SESSION_ID>`, opens the transcript with `Ctrl+T`, enters selection with `Esc`, and waits for the selection controls before focusing the destination. Press `Esc` or left for an older message, right for a newer one, and Enter to confirm.
5. For Claude, it runs `claude --resume <SESSION_ID> --fork-session`, opens Rewind with `Esc`, `Esc`, and waits for the checkpoint list before focusing the destination. Use up/down and Enter to choose a prompt, then choose **Restore conversation** to rewind only the forked conversation while preserving the current files. Choosing either code restore option intentionally changes files in the shared cwd.

## Failure behavior

The action refuses to continue when the focused pane is not Codex or Claude, Herdr did not report the matching trusted session ID, the cwd is unusable, the native fork fails to launch, Herdr reports the source ID in the new pane, or the expected picker does not appear. It shows a Herdr notification and writes the full error to the plugin command log:

```sh
herdr plugin log list --plugin local.codex-fork-from-message --limit 5
```

If a failure happens after destination creation, the action closes only the incomplete pane, tab, or workspace it created. It never falls back to a fresh conversation. An agent may retain a native fork record if failure occurs after the agent itself created the fork; this project deliberately does not edit or delete session files.

Codex can pause new sessions when hooks need trust review. The action detects that screen, removes the incomplete destination, and asks you to review the hooks in Codex before retrying. It never bypasses Codex's hook trust protection. An explicitly configured `CODEX_HOME` is preserved in the destination.

## Limitations

- Codex 0.146.1 exposes native forking by session ID but no supported API for selecting an exact earlier message. The user therefore chooses the turn in Codex's native transcript/backtrack UI.
- Codex 0.146.1 defers the fork's `SessionStart` hook until the first post-fork prompt. The action cannot require the new Herdr session ID before opening the picker; it instead relies on the successful native `codex fork <exact-session-id>` command and picker rendering. If an ID is already reported, the action verifies that it differs from the source.
- Picker verification recognizes Codex 0.146.1's transcript `edit prev` hint and its selection-only `edit next` / `edit message` hints. A future Codex release that changes these controls will fail visibly and clean up the incomplete destination instead of pretending success.
- Claude selection is native Rewind inside a native `--fork-session` branch. Rewind presents code-restoration choices as well as conversation restoration; the plugin deliberately leaves that safety-sensitive choice to the user.
- Claude's fork and Rewind UI are local and can open while logged out, but continuing with a model prompt requires a valid Claude login, subscription, API key, or supported third-party provider.
- A non-default `CLAUDE_CONFIG_DIR` is preserved when it is available to the plugin process. A value set only inside the source shell cannot currently be recovered through Herdr's pane metadata. The normal `~/.claude` configuration is supported.
- Herdr plugin actions are detached commands. Detailed errors live in Herdr's local plugin log; no telemetry, network requests, or developer callbacks are used.

## Test

```sh
./scripts/check.sh
```

The standard-library test suite covers native Codex and Claude launch arguments, all three destinations, split-direction selection, command ordering, unsupported panes, absent or hostile session IDs, mismatched integration sources, deferred and mismatched fork metadata, shell-start races, hook review, picker timeouts, and destination-specific cleanup. Live verification should use a disposable source session, confirm the fork reports a different session ID and shows the native picker, verify the source ID remains unchanged, and remove the disposable topology afterward.

## Uninstall

Remove both matching `[[keys.command]]` blocks from `~/.config/herdr/config.toml`, then run:

```sh
herdr config check
herdr server reload-config
herdr plugin unlink local.codex-fork-from-message
```

Unlinking leaves this standalone project folder untouched.
