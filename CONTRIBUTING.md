# Contributing

Issues and focused pull requests are welcome.

## Development setup

Requirements are Python 3.10 or newer, Bun and Biome. Live verification requires
Herdr 0.9.5 or newer and the selected agent CLI.

```sh
git clone https://github.com/seungwonme/herdr-fork-from-message.git
cd herdr-fork-from-message
./scripts/check.sh
```

The project intentionally has no third-party runtime or test dependencies.

## Making changes

1. Add or update a focused unit test for behavior changes.
2. Keep native agent forking intact; do not edit agent transcript/session files.
3. Preserve clear failures and leave partially started destinations available.
4. Run `./scripts/check.sh`.
5. Review the diff for unrelated changes before opening a pull request.

Live tests must use a disposable session. Verify the picker appears in the new
destination, the source session remains open with the same ID, and only the
disposable topology is removed afterward.

Use a conventional commit subject such as `fix: handle changed picker marker`.

## Pull requests

Describe the user-visible change, the failure case it addresses, and the exact
verification performed. Avoid bundling formatting or unrelated refactors with a
behavior change.

## Releasing

1. Update `version` in `herdr-plugin.toml`.
2. Move the relevant changelog entries under a dated version heading.
3. Run `./scripts/check.sh` and verify a disposable live fork when behavior changed.
4. Commit the release, create the matching `v<version>` tag, and publish that tag.
