#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 - <<'PY'
from pathlib import Path

for path in (Path("scripts/fork_from_message.py"), Path("tests/test_fork_from_message.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

if command -v herdr >/dev/null 2>&1; then
    herdr --version
fi
if command -v codex >/dev/null 2>&1; then
    codex --version
fi
