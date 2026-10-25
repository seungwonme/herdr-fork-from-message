# Changelog

All notable changes to this project will be documented in this file. The
project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-10-25

- Personal question-fork actions for Claude, Codex, OMP and GJC.
- Native current-point forks with child-only question policy and read-only tools.
- Exact owner bridge for GJC/OMP, live model settings and stale-identity refusal.
- Preserve failed destinations and distinguish pending Codex identity from success.
- Runtime guard tests and macOS native fork QA; no global rules or existing hook edits.

## [0.3.0] - 2026-08-10

### Added

- Native Codex and Claude Code session forking from Herdr's trusted session ID.
- New-tab, split-pane, and new-workspace destinations.
- Automatic opening and verification of each agent's earlier-message picker.
- Visible failures and destination-specific cleanup without transcript mutation.
- A dependency-free test and verification harness.

[Unreleased]: https://github.com/seungwonme/herdr-fork-from-message/compare/v0.4.0...HEAD
[0.3.0]: https://github.com/dmangla3/herdr-fork-from-message/releases/tag/v0.3.0
