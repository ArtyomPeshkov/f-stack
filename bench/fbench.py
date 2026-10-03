#!/usr/bin/env python3
"""fbench entry point: python3 fbench.py --help"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fbench.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
