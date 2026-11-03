#!/usr/bin/env bash
# Install tools needed by extract_super.py on Ubuntu / Debian / WSL.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo 'ERROR: Run this from Ubuntu / Debian / WSL Linux, not Windows PowerShell.' >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo 'ERROR: This installer supports Ubuntu/Debian (apt-get) only.' >&2
  exit 1
fi

SUDO=()
if [[ "$EUID" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
  else
    echo 'ERROR: sudo is not installed. Run this script as root.' >&2
    exit 1
  fi
fi

if grep -qi microsoft /proc/version 2>/dev/null; then
  echo '[INFO] Running under Windows Subsystem for Linux (WSL).'
fi

echo '[1/3] Updating apt package index...'
"${SUDO[@]}" apt-get update

echo '[2/3] Installing Python, Android sparse conversion, EROFS and ext4 tools...'
"${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 erofs-utils android-sdk-libsparse-utils e2fsprogs

echo '[3/3] Checking tools...'
for command_name in python3 simg2img fsck.erofs debugfs; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '  OK   %-12s %s\n' "$command_name" "$(command -v "$command_name")"
  else
    echo "  MISSING  $command_name" >&2
    exit 1
  fi
done

echo
printf 'Tools installed. Run:\n  python3 ./extract_super.py "./super.img"\n'
echo 'Note: EROFS directory extraction requires fsck.erofs with --extract=DIR support.'
