#!/bin/bash
set -euxo pipefail

uv run ruff format . --check
uv run ruff check .
uvx yamllint .

if command -v shellcheck &> /dev/null; then
  shellcheck scripts/*.sh
else
  echo "⚠️  shellcheck is not installed. Can't check shell scripts"
fi
