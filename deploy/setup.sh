#!/usr/bin/env bash
# Prepare files only. Does not invoke Docker, install software, or change DNS.
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$script_dir/prepare.py" --mode fresh --root "${1:-/opt/bot-combined}"
