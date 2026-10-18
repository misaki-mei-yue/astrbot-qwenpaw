"""Start the local host updater without installing this source package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle_updater.daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
