# Herdr: Fork Codex from message

A local-only Herdr action that forks the Codex session in the focused pane into a new tab, then opens Codex's native transcript picker. The original pane is never resumed, rewritten, sent keys, or closed.

## Requirements

- Herdr 0.8.0 or newer
- Codex CLI 0.146.1 or a compatible version with `codex fork <SESSION_ID>` and the native transcript overlay
- Python 3.10 or newer
- Herdr's Codex integration installed and reporting `agent_session` metadata

Check the integration with:

```sh
herdr integration status
```

If Codex is missing, install the integration and restart Codex:

```sh
herdr integration install codex
```

## Install or link locally

Run the tests, then link this folder:

```sh
./scripts/check.sh
herdr plugin link "$PWD"
herdr plugin action list --plugin local.codex-fork-from-message
```

The action is named **Fork Codex from message** and its qualified ID is `local.codex-fork-from-message.fork`.

To add a keybinding, first inspect `~/.config/herdr/config.toml` and `herdr --default-config` for conflicts. Then append a free binding such as:

```toml
[[keys.command]]
key = "prefix+f"
type = "plugin_action"
command = "local.codex-fork-from-message.fork"
description = "Fork Codex from message"
```

Validate and reload the configuration:

```sh
herdr config check
herdr server reload-config
```

## Use

1. Focus a Herdr pane running the Codex conversation to branch.
2. Invoke **Fork Codex from message** through its configured keybinding or run:

   ```sh
   herdr plugin action invoke local.codex-fork-from-message.fork
   ```

3. The action verifies Herdr's exact Codex session ID, creates a labeled tab in the same workspace and cwd, and runs `codex fork <SESSION_ID>` through Herdr's agent launcher.
4. After the native fork reaches its TUI, the action sends `Ctrl+T`, waits for the transcript, sends `Esc`, and verifies that Codex's message-selection controls appeared before focusing the new tab.
5. The latest user message is highlighted. Press `Esc` or left to choose an older message, right to choose a newer one, and Enter to confirm. Codex performs the native rollback inside the already-forked session.

## Failure behavior

The action refuses to continue when the focused pane is not Codex, Herdr did not report a trusted Codex session ID, the cwd is unusable, Codex fails to launch, Herdr reports the source ID in the new pane, or the transcript picker does not appear. It shows a Herdr notification and writes the full error to the plugin command log:

```sh
herdr plugin log list --plugin local.codex-fork-from-message --limit 5
```

If a failure happens after tab creation, the action closes only the incomplete tab it created. It never falls back to a fresh `codex` conversation. Codex may retain a native fork record if failure occurs after Codex itself created the fork; this project deliberately does not edit or delete Codex session files.

Codex can pause new sessions when hooks need trust review. The action detects that screen, removes the incomplete tab, and asks you to review the hooks in Codex before retrying. It never bypasses Codex's hook trust protection. An explicitly configured `CODEX_HOME` is preserved in the new tab.

## Limitations

- Codex 0.146.1 exposes native forking by session ID but no supported API for selecting an exact earlier message. The user therefore chooses the turn in Codex's native transcript/backtrack UI.
- Codex 0.146.1 defers the fork's `SessionStart` hook until the first post-fork prompt. The action cannot require the new Herdr session ID before opening the picker; it instead relies on the successful native `codex fork <exact-session-id>` command and picker rendering. If an ID is already reported, the action verifies that it differs from the source.
- Picker verification recognizes Codex 0.146.1's transcript `edit prev` hint and its selection-only `edit next` / `edit message` hints. A future Codex release that changes these controls will fail visibly and clean up the incomplete tab instead of pretending success.
- Herdr plugin actions are detached commands. Detailed errors live in Herdr's local plugin log; no telemetry, network requests, or developer callbacks are used.

## Test

```sh
./scripts/check.sh
```

The standard-library test suite covers the exact-session launch, command ordering, unsupported panes, absent or hostile session IDs, deferred and mismatched fork metadata, shell-start races, hook review, picker timeout, and safe cleanup. For a live disposable verification, start a new Codex conversation in a temporary Herdr tab, invoke the action from that tab, confirm the picker is visible, submit a disposable post-fork prompt, confirm the new session ID differs, and close both disposable tabs afterward.

## Uninstall

Remove the matching `[[keys.command]]` block from `~/.config/herdr/config.toml`, then run:

```sh
herdr config check
herdr server reload-config
herdr plugin unlink local.codex-fork-from-message
```

Unlinking leaves this standalone project folder untouched.
