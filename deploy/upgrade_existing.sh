#!/usr/bin/env bash
# The name denotes add-on preparation, not an automatic running-server upgrade.
# No existing compose, data, container, network, or web route is changed here.
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$script_dir/prepare.py" --mode existing \
  --root "${1:-/opt/bot/combined}" --existing-dir "${2:-/opt/bot}"
