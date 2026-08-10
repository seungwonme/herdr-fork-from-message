# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose local
conversation data, session identities, terminal contents, or arbitrary command
execution.

Report it privately through the repository's
[GitHub security advisory form](https://github.com/dmangla3/herdr-fork-from-message/security/advisories/new).
Include the affected version, agent and Herdr versions, reproduction steps, and
the expected impact. Do not include real conversation contents or credentials.

For non-sensitive bugs, use the public issue tracker.

## Security model

This plugin controls local Herdr terminals under the current user's permissions.
It accepts session identity only from Herdr's matching Codex or Claude integration,
passes native arguments without a shell, and does not edit transcript files or send
telemetry. A forked agent retains its normal access to the focused pane's working
directory and may contact its configured model provider after the user submits a
prompt.
