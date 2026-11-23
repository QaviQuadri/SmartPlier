"""Source-checkout entry point for Smartplier."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from smartplier.jobhunt import main

if __name__ == "__main__":
    raise SystemExit(main())
