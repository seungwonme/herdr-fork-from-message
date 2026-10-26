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
Claude/Codex/OpenCode/Copilot source identity comes from matching Herdr integrations. GJC/OMP/Pi/OMO
identity comes from owner-written private records bound to the server socket,
terminal, foreground PID/process-start and cwd. Records expire and are rechecked.
The bridge never exports credentials, scans process environments, or rewrites JSONL.

Native process arguments use Herdr's launcher; Pi-family agents use a private temporary POSIX
script. Failed destinations are preserved so approval/login state is not destroyed.
Normal Panel Fork uses native permissions, tools, hooks and MCP settings; restored
goals can resume according to the agent's behavior. Optional read-only mode
restricts model tools, not arbitrary process filesystem access. The
checkout is shared. Trusted runtime extensions and manually changed permissions
remain outside this boundary. Provider calls use the agent's normal configuration
after the user asks a question; the plugin has no telemetry or remote callbacks.

Cursor, Antigravity and Hermes automatic forks fail before destination creation.
They are never implemented as same-ID resume, child prompt injection, direct
transcript edits, or speculative storage import. Hermes environment/storage repair
is outside this plugin. OpenCode/Copilot adapters require the invoking configuration
roots to match their source; that equivalence is not proved by an owner descriptor.

Copilot uses a private headless process and the native experimental `sessions.fork`
RPC. It never resumes the source. Native Copilot does append a source fork-info
event, so byte-for-byte source immutability is not promised. The process has bounded
stdio framing, a shared deadline and disabled client callbacks, and is reaped before
the child is opened. Returned IDs are checked against source and observed child IDs.
Copilot itself can perform its normal startup/auth/network behavior; callback denial
is not an OS/network sandbox and the adapter does not bypass login requirements.
