#!/usr/bin/env bash
# Linux launcher: finds Python 3 and hands over to start.py (which checks Pillow and starts the patcher).
cd "$(dirname "$(readlink -f "$0")")" || exit 1
PY=$(command -v python3 || command -v python) || {
    echo "Python 3.10 or newer is required (e.g. 'sudo apt install python3 python3-venv')." >&2
    exit 1
}
exec "$PY" start.py "$@"
