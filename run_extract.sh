#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( $# == 0 )); then
  exec python3 "$SCRIPT_DIR/extract_super.py" "$SCRIPT_DIR/super.img"
else
  exec python3 "$SCRIPT_DIR/extract_super.py" "$@"
fi
