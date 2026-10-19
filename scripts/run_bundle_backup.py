"""Dedicated host entry; no HTTP listener or Docker socket exposed to the browser."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle_updater.backup_daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
