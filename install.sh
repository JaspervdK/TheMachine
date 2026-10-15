#!/bin/sh
# One entry point for installing, upgrading and removing The Machine. See ./install.sh --help.
set -e
cd "$(dirname "$0")"
for py in "${PYTHON:-}" python3.14 python3.13 python3.12 python3; do
  [ -n "$py" ] || continue
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    exec "$py" installer.py "$@"
  fi
done
echo "The Machine needs Python 3.12 or newer (python3 --version). Install it, or set PYTHON=/path/to/python3.12." >&2
exit 1
