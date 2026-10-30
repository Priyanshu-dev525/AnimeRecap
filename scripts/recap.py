#!/usr/bin/env python3
"""animerecap command line.

    python scripts/recap.py tools
    python scripts/recap.py <tool> '<json or path-to.json>'

The engine lives in the recap/ package next to this file.
"""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("animerecap needs Python 3.10 or newer.")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recap.cli import main  # noqa: E402  (a package directory wins over this file's own name)

if __name__ == "__main__":
    sys.exit(main())
