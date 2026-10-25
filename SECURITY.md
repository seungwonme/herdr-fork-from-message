# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose local
conversation data, session identities, terminal contents, or arbitrary command
execution.

Report it privately through the repository's
[GitHub security advisory form](https://github.com/seungwonme/herdr-fork-from-message/security/advisories/new).
Include the affected version, agent and Herdr versions, reproduction steps, and
the expected impact. Do not include real conversation contents or credentials.

For non-sensitive bugs, use the public issue tracker.

## Security model

This plugin controls local Herdr terminals under the current user's permissions.
Claude/Codex source identity comes from their matching Herdr integrations. GJC/OMP
identity comes from owner-written private records bound to the server socket,
terminal, foreground PID/process-start and cwd. Records expire and are rechecked.
The bridge never exports credentials, scans process environments, or rewrites JSONL.

Native process arguments use Herdr's launcher; GJC uses a private temporary POSIX
script. Failed destinations are preserved so approval/login state is not destroyed.
Normal Panel Fork uses native permissions, tools, hooks and MCP settings; restored
goals can resume according to the agent's behavior. Optional read-only mode
restricts model tools, not arbitrary process filesystem access. The
checkout is shared. Trusted runtime extensions and manually changed permissions
remain outside this boundary. Provider calls use the agent's normal configuration
after the user asks a question; the plugin has no telemetry or remote callbacks.
